import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

# .env의 실제 DB에 붙지 않게 app을 불러오기 전에 비운다
os.environ["DATABASE_URL"] = ""
os.environ["IDLY_DB"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import billing  # noqa: E402

TOKEN = "g" * 40
SAMPLE = json.loads((app.HERE / "sample_report.json").read_text(encoding="utf-8"))


def make_report(created=None):
    """이 브라우저(게스트)가 만든, 분석이 끝난 리포트."""
    data = dict(SAMPLE)
    data.pop("sample", None)
    report = app.Report("me@example.com", None, data=data, guest=app.hashlib.sha256(TOKEN.encode()).hexdigest())
    report.created = created or datetime.now()
    return report


class PayWhatYouWantTests(unittest.TestCase):
    def setUp(self):
        app._reports.clear()
        self.client = TestClient(app.app)
        self.headers = {app.GUEST_HEADER: TOKEN}
        # 결제를 켠 뒤의 리포트로 본다
        self.cutoff = patch.object(app, "PAYWALL_FROM", datetime.now() - timedelta(days=1))
        self.cutoff.start()
        self.addCleanup(self.cutoff.stop)

    def add(self, report):
        report.paid = app._first_report(report)
        app._reports[report.id] = report
        return report

    def test_first_report_is_free_second_asks(self):
        first = self.add(make_report())
        second = self.add(make_report())
        self.assertTrue(app._open(first))
        self.assertFalse(app._open(second))
        r = self.client.get(f"/api/reports/{second.id}/view", headers=self.headers).json()
        self.assertFalse(r["paid"])
        # 금액을 고르기 전에는 PDF도 받을 수 없다
        self.assertEqual(self.client.get(f"/api/reports/{second.id}/pdf", headers=self.headers).status_code, 402)

    def paddle(self):
        """실서비스처럼 Paddle을 켠 상태 (4,900원 가격 하나 + 금액별 할인 코드)."""
        for target, name, value in [(app, "PAYMENT_MODE", "paddle"), (billing, "PRICE_ID", "pri_4900"),
                                    (billing, "CLIENT_TOKEN", "live_x")]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)

    def checkout(self, report, amount):
        return self.client.post(f"/api/reports/{report.id}/checkout", json={"amount": amount}, headers=self.headers)

    def test_each_amount_gets_its_discount_code(self):
        self.paddle()
        self.add(make_report())
        second = self.add(make_report())
        self.assertEqual(app.prices()["amounts"], [0, 900, 1900, 2900, 3900, 4900])
        expected = {0: "teampass", 900: "discount0", 1900: "discount1", 2900: "discount2", 3900: "discount3", 4900: None}
        for amount, code in expected.items():
            body = self.checkout(second, amount).json()
            self.assertFalse(body["paid"], amount)
            self.assertEqual(body["paddle"]["priceId"], "pri_4900")
            self.assertEqual(body["paddle"]["discountCode"], code, amount)
        # 결제창만 열었을 뿐 아직 열리지 않는다 (웹훅이 거래를 확인해야 열린다)
        self.assertFalse(app._open(second))

    def test_unknown_amount_is_rejected(self):
        self.paddle()
        self.add(make_report())
        second = self.add(make_report())
        self.assertEqual(self.checkout(second, 1000).status_code, 400)

    def test_without_paddle_config_nothing_to_pick(self):
        with patch.object(app, "PAYMENT_MODE", "paddle"), patch.object(billing, "PRICE_ID", ""):
            self.assertEqual(app.prices()["amounts"], [])

    def test_mock_mode_opens_at_once(self):
        self.add(make_report())
        second = self.add(make_report())
        with patch.object(app, "PAYMENT_MODE", "mock"):
            self.assertEqual(self.checkout(second, 0).json(), {"paid": True})
        self.assertTrue(app._open(second))

    def test_reports_from_free_period_stay_open(self):
        self.cutoff.stop()
        old = make_report(created=datetime.now() - timedelta(days=3))
        old.paid = False
        app._reports[old.id] = old
        with patch.object(app, "PAYWALL_FROM", datetime.now() - timedelta(days=1)):
            self.assertTrue(app._open(old))
            # 무료 기간에 만든 리포트도 한 번으로 센다: 다음 리포트는 금액을 고른다
            self.assertFalse(app._open(self.add(make_report())))
        self.cutoff.start()

    def test_webhook_accepts_zero_total_from_discount(self):
        raw = json.dumps({"event_type": "transaction.completed", "data": {
            "id": "txn_1", "custom_data": {"report_id": "r1", "amount": 0}, "currency_code": "KRW",
            "items": [{"price": {"id": "pri_4900"}}], "details": {"totals": {"grand_total": "0"}}}}).encode()
        with patch.object(billing, "PRICE_ID", "pri_4900"):
            event = billing.parse_webhook(raw)
        self.assertEqual(event["report_id"], "r1")
        self.assertEqual(event["amount"], 0)
        self.assertEqual(event["status"], "paid")


if __name__ == "__main__":
    unittest.main()
