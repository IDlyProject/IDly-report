import copy
import hashlib
import json
import secrets
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import db

SAMPLE = json.loads((app.HERE / "sample_report.json").read_text(encoding="utf-8"))


def report_data():
    data = copy.deepcopy(SAMPLE)
    data.pop("sample", None)
    for account in data["accounts"]:
        account.pop("actions", None)
        for insight in account.get("insights", []):
            insight.pop("action", None)
    return data


class AppLinkTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.upgrade = patch.object(app, "upgrade_report", return_value=0)
        self.upgrade_mock = self.upgrade.start()
        # 백그라운드 작업을 바로 실행해 테스트가 결과를 기다리지 않게 한다
        patch.object(app, "_run_background", lambda fn: fn()).start()
        self.pdf = patch.object(app.Report, "prepare_pdf", lambda self: None)
        self.pdf.start()

    def tearDown(self):
        patch.stopall()
        self.client.close()

    def guest_report(self, paid=False):
        token = secrets.token_urlsafe(32)
        report = app.Report("me@example.com", None, data=report_data(),
                            guest=hashlib.sha256(token.encode()).hexdigest())
        report.paid = paid
        app._reports[report.id] = report
        return report, {app.GUEST_HEADER: token}

    def user_reports(self, n=2):
        user = db.upsert_user("dev", secrets.token_hex(6), "me@example.com", "me")
        reports = []
        for _ in range(n):
            report = app.Report("Mail@Example.com", user["id"], data=report_data())
            app._reports[report.id] = report
            report.save()
            reports.append(report)
        return user, reports

    def test_mock_checkout_triggers_upgrade(self):
        report, headers = self.guest_report()
        with patch.object(app, "PAYMENT_MODE", "mock"):
            response = self.client.post(f"/api/reports/{report.id}/checkout", headers=headers)
        self.assertEqual(response.json(), {"paid": True})
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def test_dev_confirm_triggers_upgrade(self):
        report, headers = self.guest_report()
        with patch.object(app, "DEBUG", True):
            response = self.client.post(f"/api/reports/{report.id}/dev-confirm", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def paddle(self, report, status, order_id):
        event = {"event": "transaction.completed", "order_id": order_id, "report_id": report.id, "user_id": None,
                 "status": status, "amount": 4900, "currency": "KRW"}
        with patch.object(app.billing, "verify_signature", return_value=True), \
                patch.object(app.billing, "parse_webhook", return_value=event):
            return self.client.post("/api/webhooks/paddle", content=b"{}")

    def test_paddle_paid_webhook_triggers_upgrade(self):
        report, _ = self.guest_report()
        report.save()
        self.assertEqual(self.paddle(report, "paid", "txn_" + report.id).status_code, 200)
        self.assertTrue(report.paid)
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def test_refund_does_not_upgrade_or_revert(self):
        report, _ = self.guest_report()
        report.save()
        self.paddle(report, "paid", "txn_r" + report.id)
        report.data["accounts"][0]["actions"] = {"해지": {"url": "https://x.com/c", "source": "search"}}
        self.paddle(report, "refunded", "txn_r" + report.id)
        self.assertFalse(report.paid)
        self.assertEqual(self.upgrade_mock.call_count, 1)
        self.assertEqual(report.data["accounts"][0]["actions"]["해지"]["source"], "search")

    def test_payment_upgrades_every_newly_paid_report_once(self):
        user, reports = self.user_reports(2)
        with patch.object(app, "DEBUG", True), patch.object(app, "_user", return_value=user):
            self.client.post(f"/api/reports/{reports[0].id}/dev-confirm")
            self.client.post(f"/api/reports/{reports[0].id}/dev-confirm")
        self.assertTrue(all(r.paid for r in reports))
        upgraded = [call.args[0] for call in self.upgrade_mock.call_args_list]
        self.assertEqual(len(upgraded), 2)
        self.assertEqual({id(d) for d in upgraded}, {id(r.data) for r in reports})

    def test_unpaid_report_never_searches(self):
        report, _ = self.guest_report()
        app._finish_report(report, report.data)
        self.assertEqual(self.upgrade_mock.call_count, 0)
        subs = [a for a in report.data["accounts"] if a.get("subscription")]
        self.assertTrue(subs and all("actions" in a for a in subs))

    def test_paid_at_start_searches_on_done(self):
        report, _ = self.guest_report(paid=True)
        app._finish_report(report, report.data)
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def test_payment_during_attach_waits_for_finish(self):
        token_report = app.Report("me@example.com", None, guest="g" * 64)
        data = report_data()
        token_report._data = data  # 분석 결과가 나왔지만 링크를 정하는 중 (job.report는 on_done 전에 생긴다)
        token_report.paid = True
        app._upgrade_links(token_report)
        self.assertEqual(self.upgrade_mock.call_count, 0)
        app._finish_report(token_report, data)
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def test_upgrade_attaches_links_for_old_reports(self):
        report, _ = self.guest_report(paid=True)
        self.assertNotIn("actions", report.data["accounts"][2])
        app._upgrade_links(report)
        self.assertIn("actions", report.data["accounts"][2])
        self.assertEqual(self.upgrade_mock.call_count, 1)

    def test_upgrade_failure_keeps_report(self):
        self.upgrade_mock.side_effect = RuntimeError("boom")
        report, _ = self.guest_report(paid=True)
        app._finish_report(report, report.data)
        self.assertIsNotNone(report.data)
        self.assertTrue(report._links_upgraded)

    def test_attach_failure_keeps_report(self):
        report, _ = self.guest_report()
        with patch.object(app, "attach_offline", side_effect=RuntimeError("boom")), \
                patch.object(report, "save") as save:
            app._finish_report(report, report.data)
        save.assert_called_once()

    def test_changed_links_save_and_regenerate_pdf(self):
        self.upgrade_mock.return_value = 2
        report, _ = self.guest_report(paid=True)
        report._pdf = b"old"
        with patch.object(report, "save") as save:
            app._upgrade_links(report)
        save.assert_called_once()
        self.assertIsNone(report._pdf)

    def test_invalidate_pdf_waits_for_running_render(self):
        report, _ = self.guest_report()
        report._pdf = b"old"
        report._pdf_lock.acquire()
        worker = threading.Thread(target=report.invalidate_pdf)
        worker.start()
        time.sleep(0.05)
        self.assertEqual(report._pdf, b"old")
        report._pdf_lock.release()
        worker.join(1)
        self.assertIsNone(report._pdf)


if __name__ == "__main__":
    unittest.main()
