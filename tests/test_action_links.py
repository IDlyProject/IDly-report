import unittest
from datetime import datetime, timedelta
from urllib.parse import quote_plus

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


if __name__ == "__main__":
    unittest.main()
