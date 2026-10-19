import json
import re
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import action_links as al
import report_view as rv

NOW = datetime(2026, 10, 19)


def account(domain, service, **extra):
    base = {"id": domain, "service": service, "category": "OTT", "signupDate": "2024-01-01", "unused": False,
            "recommended": None, "subscription": None, "insights": [], "permissions": []}
    base.update(extra)
    return base


def sub(amount=17000):
    return {"status": "활성", "cycle": "월간", "monthly": amount, "amount": amount, "nextBilling": "2026-11-01"}


def report(*accounts):
    return {"messages_scanned": 1000, "accounts": list(accounts), "connected_apps": []}


def buttons(html):
    return re.findall(r'<a class="rp-action[^"]*"[^>]*>[^<]*</a>', html)


class RenderActionTests(unittest.TestCase):
    def data(self):
        data = report(
            account("netflix.com", "넷플릭스", subscription=sub()),
            account("old-svc.com", "옛서비스", unused=True, recommended="탈퇴", lastSeen="2023-01-01"),
            account("sec-svc.com", "보안서비스", insights=[{"kind": "보안", "date": "2026-10-01", "status": "새 기기",
                                                         "advice": "비밀번호 점검"}]),
        )
        al.attach_offline(data)
        return data

    def test_full_report_has_buttons_in_three_sections(self):
        html = rv.render_fragment(self.data(), NOW, full=True)
        found = buttons(html)
        self.assertEqual(len(found), 3, found)
        self.assertTrue(any("해지하러 가기" in b for b in found))
        self.assertTrue(any("탈퇴 방법 찾아보기" in b for b in found))
        self.assertTrue(any("보안 설정 방법 찾아보기" in b for b in found))

    def test_locked_preview_has_no_buttons(self):
        html = rv.render_fragment(self.data(), NOW, full=False)
        self.assertNotIn("rp-action", html)

    def test_fallback_label_differs(self):
        data = report(account("svc-a.com", "에이", subscription=sub()))
        data["accounts"][0]["actions"] = {"해지": al.fallback_link("에이", "해지")}
        [button] = buttons(rv.render_fragment(data, NOW, full=True))
        self.assertIn("해지 방법 찾아보기", button)
        self.assertIn("is-search", button)

    def test_button_escapes_url_and_label(self):
        data = report(account("svc-a.com", '<b>&"', subscription=sub()))
        html = rv.render_fragment(data, NOW, full=True)
        [button] = buttons(html)
        self.assertNotIn("<b>", button)
        self.assertIn("q=%3Cb%3E%26%22", button)

    def test_links_have_noopener(self):
        for button in buttons(rv.render_fragment(self.data(), NOW, full=True)):
            self.assertIn('target="_blank"', button)
            self.assertIn('rel="noopener noreferrer"', button)

    def test_unused_with_subscription_recommendation_gets_cancel(self):
        data = report(account("svc-a.com", "에이", unused=True, recommended="구독 해지"))
        [button] = buttons(rv.render_fragment(data, NOW, full=True))
        self.assertIn("해지 방법 찾아보기", button)

    def test_old_report_without_actions_still_has_buttons(self):
        data = report(
            account("netflix.com", "넷플릭스", subscription=sub()),
            account("google.com", "Google", insights=[{"kind": "보안", "date": "2026-10-01", "status": "s",
                                                      "advice": "myaccount.google.com/device-activity에서 로그아웃"}]),
        )
        with patch.object(al.db, "get_action_link", side_effect=AssertionError("DB 사용 금지")):
            html = rv.render_fragment(data, NOW, full=True)
        found = buttons(html)
        self.assertEqual(len(found), 2)
        self.assertIn("https://myaccount.google.com/device-activity", html)
        self.assertIn(al.CURATED["netflix.com"]["해지"][0], html)

    def test_buttons_send_ga_event_without_service_name(self):
        found = buttons(rv.render_fragment(self.data(), NOW, full=True))
        attrs = [dict(re.findall(r'(data-ga[\w-]*)="([^"]*)"', b)) for b in found]
        self.assertEqual(attrs, [
            {"data-ga": "action_click", "data-ga-action": "cancel", "data-ga-source": "curated"},
            {"data-ga": "action_click", "data-ga-action": "security", "data-ga-source": "fallback"},
            {"data-ga": "action_click", "data-ga-action": "delete", "data-ga-source": "fallback"},
        ])
        for b in found:
            self.assertNotIn("넷플릭스", re.sub(r'href="[^"]*"', "", b))

    def test_sample_report_renders_curated_buttons(self):
        data = json.loads((Path(rv.__file__).parent / "sample_report.json").read_text(encoding="utf-8"))
        with patch.object(al.db, "get_action_link", side_effect=AssertionError("DB 사용 금지")):
            html = rv.render_fragment(data, NOW, full=True)
        found = buttons(html)
        self.assertTrue(found)
        netflix = [b for b in found if al.CURATED["netflix.com"]["해지"][0] in b]
        self.assertEqual(len(netflix), 1)
        for a in data["accounts"]:
            self.assertTrue(a.get("actions") or not al.needed_actions(a), a["service"])


if __name__ == "__main__":
    unittest.main()
