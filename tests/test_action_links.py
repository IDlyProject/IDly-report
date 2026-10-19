import json
import threading
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from urllib.parse import quote_plus

import httpx

import action_links as al
import db


def clear_cache():
    db._exec("DELETE FROM action_links")


class ValidateTests(unittest.TestCase):
    def test_validate_accepts_domain_subdomain_and_allowed(self):
        self.assertTrue(al.is_valid("https://netflix.com/cancelplan", "netflix.com"))
        self.assertTrue(al.is_valid("https://www.netflix.com/cancelplan", "netflix.com"))
        self.assertTrue(al.is_valid("https://www.google.com/youtube/x", "youtube.com"))

    def test_validate_rejects_unsafe(self):
        for url in ["http://www.netflix.com/x", "https://user:pw@netflix.com/x", "https://netflix.com:8443/x",
                    "https://netflix.com:443/x", "https://1.2.3.4/x", "https://[::1]/x", "https://other.com/x",
                    "javascript:alert(1)", "", "https://netflix.com:abc/x"]:
            self.assertFalse(al.is_valid(url, "netflix.com"), url)

    def test_validate_rejects_lookalikes(self):
        self.assertFalse(al.is_valid("https://netflix.com.evil.io/x", "netflix.com"))
        self.assertFalse(al.is_valid("https://evilnetflix.com/x", "netflix.com"))
        self.assertTrue(al.is_valid("https://NETFLIX.COM./x", "netflix.com"))

    def test_base_domain(self):
        self.assertEqual(al.base_domain("apps.apple.com/claude"), "apple.com")
        self.assertEqual(al.base_domain("Netflix.com"), "netflix.com")


class ResolveOfflineTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_curated_wins_over_cache(self):
        db.put_action_link("netflix.com", "해지", "https://www.netflix.com/other", "search")
        link = al.resolve_offline({"id": "netflix.com", "service": "넷플릭스"}, "해지")
        self.assertEqual(link["source"], "curated")
        self.assertEqual(link["url"], al.CURATED["netflix.com"]["해지"][0])

    def test_cache_used_when_not_curated(self):
        db.put_action_link("unknown-svc.com", "탈퇴", "https://unknown-svc.com/leave", "search")
        link = al.resolve_offline({"id": "unknown-svc.com", "service": "언노운"}, "탈퇴")
        self.assertEqual(link, {"url": "https://unknown-svc.com/leave", "source": "cache"})

    def test_cache_expired_after_90_days(self):
        db.put_action_link("unknown-svc.com", "탈퇴", "https://unknown-svc.com/leave", "search")
        old = (datetime.now() - timedelta(days=91)).isoformat(timespec="seconds")
        db._exec("UPDATE action_links SET found_at = ?", (old,))
        link = al.resolve_offline({"id": "unknown-svc.com", "service": "언노운"}, "탈퇴")
        self.assertEqual(link["source"], "fallback")

    def test_invalid_cached_url_is_ignored(self):
        db.put_action_link("unknown-svc.com", "탈퇴", "https://evil.io/leave", "search")
        link = al.resolve_offline({"id": "unknown-svc.com", "service": "언노운"}, "탈퇴")
        self.assertEqual(link["source"], "fallback")

    def test_cache_skipped_when_use_cache_false(self):
        db.put_action_link("unknown-svc.com", "탈퇴", "https://unknown-svc.com/leave", "search")
        link = al.resolve_offline({"id": "unknown-svc.com", "service": "언노운"}, "탈퇴", use_cache=False)
        self.assertEqual(link["source"], "fallback")

    def test_fallback_url_is_google_search_with_encoded_query(self):
        link = al.fallback_link("넷플릭스", "해지")
        self.assertEqual(link, {"url": "https://www.google.com/search?q=" + quote_plus("넷플릭스 해지 방법"),
                                "source": "fallback"})

    def test_apple_app_store_subscription(self):
        link = al.resolve_offline({"id": "apps.apple.com/claude", "service": "Claude"}, "해지")
        self.assertEqual(link, {"url": "https://apps.apple.com/account/subscriptions", "source": "curated"})

    def test_labels(self):
        self.assertEqual(al.label("해지", "curated"), "해지하러 가기")
        self.assertEqual(al.label("해지", "fallback"), "해지 방법 찾아보기")
        self.assertEqual(al.label("탈퇴", "cache"), "탈퇴하러 가기")
        self.assertEqual(al.label("탈퇴", "fallback"), "탈퇴 방법 찾아보기")
        self.assertEqual(al.label("보안", "search"), "보안 설정 열기")
        self.assertEqual(al.label("보안", "fallback"), "보안 설정 방법 찾아보기")


def sub_account(domain, service="서비스", status="활성", **extra):
    account = {"id": domain, "service": service, "email": "me@example.com", "unused": False, "recommended": None,
               "subscription": {"status": status, "cycle": "월간", "monthly": 10000}, "insights": []}
    account.update(extra)
    return account


def openai_reply(text, citations=()):
    return {"output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "content": [{"type": "output_text", "text": text,
                                         "annotations": [{"type": "url_citation", "url": u} for u in citations]}]},
    ]}


class FakeOpenAI:
    """httpx.post 대역. reply(body) -> (status, json) 또는 예외."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, **kwargs):
        with self.lock:
            self.calls.append((url, kwargs))
        result = self.reply(kwargs.get("json") or {})
        if isinstance(result, Exception):
            raise result
        status, body = result
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


class AttachTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def test_needed_actions(self):
        self.assertEqual(al.needed_actions(sub_account("a.com")), ["해지"])
        self.assertEqual(al.needed_actions(sub_account("a.com", status="만료 예정")), ["해지"])
        self.assertEqual(al.needed_actions({"id": "a.com", "unused": True, "recommended": "구독 해지"}), ["해지"])
        self.assertEqual(al.needed_actions({"id": "a.com", "unused": True, "recommended": "탈퇴"}), ["탈퇴"])
        self.assertEqual(al.needed_actions({"id": "a.com", "unused": True, "recommended": None}), ["탈퇴"])
        self.assertEqual(al.needed_actions(sub_account("a.com", unused=True, recommended="구독 해지")), ["해지"])
        self.assertEqual(al.needed_actions(sub_account("a.com", status="해지됨")), [])

    def test_attach_offline_sets_every_needed_slot_and_security_insights(self):
        report = {"accounts": [
            sub_account("netflix.com", "넷플릭스"),
            {"id": "old-svc.com", "service": "옛서비스", "unused": True, "recommended": None, "insights": []},
            {"id": "sec-svc.com", "service": "보안서비스", "unused": False, "insights": [
                {"kind": "보안", "status": "s", "advice": "비밀번호 점검"}, {"kind": "휴면", "status": "s", "advice": "a"}]},
        ]}
        fake = FakeOpenAI(lambda body: (200, openai_reply("NONE")))
        with patch.object(al.httpx, "post", fake):
            al.attach_offline(report)
        netflix, old, sec = report["accounts"]
        self.assertEqual(netflix["actions"]["해지"]["source"], "curated")
        self.assertEqual(old["actions"]["탈퇴"]["source"], "fallback")
        self.assertEqual(sec["actions"], {})
        self.assertEqual(sec["insights"][0]["action"]["source"], "fallback")
        self.assertNotIn("action", sec["insights"][1])
        self.assertEqual(fake.calls, [])

    def test_attach_offline_keeps_existing_values(self):
        existing = {"url": "https://netflix.com/x", "source": "search"}
        report = {"accounts": [sub_account("netflix.com", actions={"해지": existing})]}
        al.attach_offline(report)
        self.assertEqual(report["accounts"][0]["actions"]["해지"], existing)

    def test_google_insight_uses_specific_page(self):
        account = {"id": "google.com", "service": "Google", "insights": []}
        insight = {"kind": "보안", "advice": "myaccount.google.com/apppasswords에서 지금 쓰지 않는 앱 비밀번호 삭제"}
        self.assertEqual(al.security_link(account, insight),
                         {"url": "https://myaccount.google.com/apppasswords", "source": "curated"})
        plain = {"kind": "보안", "advice": "본인 시도였는지 확인"}
        self.assertEqual(al.security_link(account, plain)["url"], al.CURATED["google.com"]["보안"][0])


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def report(self, *accounts):
        report = {"accounts": list(accounts)}
        al.attach_offline(report)
        return report

    def test_upgrade_only_searches_fallback_slots(self):
        report = self.report(sub_account("netflix.com", "넷플릭스"), sub_account("svc-a.com", "에이"))
        fake = FakeOpenAI(lambda body: (200, openai_reply("NONE")))
        with patch.object(al.httpx, "post", fake):
            al.upgrade_report(report, api_key="k")
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("svc-a.com", json.dumps(fake.calls[0][1]["json"], ensure_ascii=False))

    def test_upgrade_replaces_and_caches_valid_result(self):
        report = self.report(sub_account("svc-a.com", "에이"))
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://www.svc-a.com/billing/cancel")))
        with patch.object(al.httpx, "post", fake):
            changed = al.upgrade_report(report, api_key="k")
        self.assertEqual(changed, 1)
        self.assertEqual(report["accounts"][0]["actions"]["해지"],
                         {"url": "https://www.svc-a.com/billing/cancel", "source": "search"})
        self.assertEqual(db.get_action_link("svc-a.com", "해지")["url"], "https://www.svc-a.com/billing/cancel")

    def test_upgrade_fills_security_insight(self):
        account = {"id": "svc-a.com", "service": "에이", "insights": [{"kind": "보안", "advice": "점검"}]}
        report = self.report(account)
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://svc-a.com/security")))
        with patch.object(al.httpx, "post", fake):
            al.upgrade_report(report, api_key="k")
        self.assertEqual(report["accounts"][0]["insights"][0]["action"]["source"], "search")

    def test_upgrade_rejects_offdomain_result_and_does_not_cache(self):
        report = self.report(sub_account("svc-a.com", "에이"))
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://blog.example.com/how-to-cancel-svc-a")))
        with patch.object(al.httpx, "post", fake):
            changed = al.upgrade_report(report, api_key="k")
        self.assertEqual(changed, 0)
        self.assertEqual(report["accounts"][0]["actions"]["해지"]["source"], "fallback")
        self.assertIsNone(db.get_action_link("svc-a.com", "해지"))

    def test_search_parses_markdown_and_trailing_punctuation(self):
        fake = FakeOpenAI(lambda body: (200, openai_reply("여기서 하세요: [해지](https://www.netflix.com/cancelplan).")))
        with patch.object(al.httpx, "post", fake):
            url = al.search_link("넷플릭스", "netflix.com", "해지", "k")
        self.assertEqual(url, "https://www.netflix.com/cancelplan")

    def test_search_uses_citation_when_text_has_no_valid_url(self):
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://evil.io/x 참고", ["https://help.svc-a.com/cancel"])))
        with patch.object(al.httpx, "post", fake):
            url = al.search_link("에이", "svc-a.com", "해지", "k")
        self.assertEqual(url, "https://help.svc-a.com/cancel")

    def test_search_http_error_falls_back(self):
        for reply in (lambda body: (500, {"error": "x"}), lambda body: httpx.TimeoutException("slow"),
                      lambda body: (200, {"unexpected": True})):
            clear_cache()
            report = self.report(sub_account("svc-a.com", "에이"))
            with patch.object(al.httpx, "post", FakeOpenAI(reply)):
                self.assertEqual(al.upgrade_report(report, api_key="k"), 0)
            self.assertEqual(report["accounts"][0]["actions"]["해지"]["source"], "fallback")

    def test_no_api_key_means_no_network(self):
        report = self.report(sub_account("svc-a.com", "에이"))
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://svc-a.com/cancel")))
        with patch.dict(al.os.environ, {"OPENAI_API_KEY": ""}), patch.object(al.httpx, "post", fake):
            self.assertEqual(al.upgrade_report(report), 0)
        self.assertEqual(fake.calls, [])

    def test_cap_twenty_searches(self):
        report = self.report(*[sub_account(f"svc-{i}.com", f"서비스{i}") for i in range(25)])
        fake = FakeOpenAI(lambda body: (200, openai_reply("NONE")))
        with patch.object(al.httpx, "post", fake):
            al.upgrade_report(report, api_key="k")
        self.assertEqual(len(fake.calls), al.MAX_SEARCHES)
        self.assertEqual(al.MAX_SEARCHES, 20)

    def test_same_domain_action_searched_once(self):
        account = {"id": "svc-a.com", "service": "에이", "insights": [{"kind": "보안", "advice": "a"},
                                                                    {"kind": "보안", "advice": "b"}]}
        report = self.report(account)
        fake = FakeOpenAI(lambda body: (200, openai_reply("https://svc-a.com/security")))
        with patch.object(al.httpx, "post", fake):
            self.assertEqual(al.upgrade_report(report, api_key="k"), 2)
        self.assertEqual(len(fake.calls), 1)

    def test_query_never_contains_user_email(self):
        report = self.report(sub_account("svc-a.com", "에이"))
        fake = FakeOpenAI(lambda body: (200, openai_reply("NONE")))
        with patch.object(al.httpx, "post", fake):
            al.upgrade_report(report, api_key="k")
        self.assertTrue(fake.calls)
        for _, kwargs in fake.calls:
            self.assertNotIn("me@example.com", json.dumps(kwargs, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
