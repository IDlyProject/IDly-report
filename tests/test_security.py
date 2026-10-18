import imaplib
import os
import socket
import ssl
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import db
import security

ROOT = Path(__file__).resolve().parent.parent
GUEST = {"X-IDly-Guest": "g" * 40}


def _addrinfo(*ips):
    def fake(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    return fake


class ResolvePublicTests(unittest.TestCase):
    def test_rejects_internal_addresses(self):
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
                   "100.64.0.1", "0.0.0.0", "::1", "fd00::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(ip=ip), patch.object(security.socket, "getaddrinfo", _addrinfo(ip)):
                with self.assertRaises(security.UnsafeHost):
                    security.resolve_public("mail.example.com", 993)

    def test_rejects_when_any_result_is_internal(self):
        with patch.object(security.socket, "getaddrinfo", _addrinfo("142.250.1.1", "127.0.0.1")):
            with self.assertRaises(security.UnsafeHost):
                security.resolve_public("mail.example.com", 993)

    def test_rejects_ports_other_than_993(self):
        with patch.object(security.socket, "getaddrinfo", _addrinfo("142.250.1.1")):
            for port in (143, 22, 6379, 8080):
                with self.subTest(port=port), self.assertRaises(security.UnsafeHost):
                    security.resolve_public("mail.example.com", port)

    def test_returns_public_ip(self):
        with patch.object(security.socket, "getaddrinfo", _addrinfo("142.250.1.1")):
            self.assertEqual(security.resolve_public("imap.gmail.com", 993), ["142.250.1.1"])


class TlsTests(unittest.TestCase):
    def test_context_verifies_certificate_and_hostname(self):
        context = security.tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_pinned_socket_uses_checked_ip_and_original_hostname(self):
        calls = {}

        class FakeContext:
            def wrap_socket(self, sock, server_hostname):
                calls["server_hostname"] = server_hostname
                return sock

        conn = object.__new__(security.PinnedIMAP4_SSL)
        conn.host, conn.port, conn.ssl_context = "imap.example.com", 993, FakeContext()
        conn._pinned_ips = ["2001:db8::1", "142.250.1.1"]

        def fake_connect(addr, timeout):
            calls.setdefault("tried", []).append(addr)
            if ":" in addr[0]:
                raise OSError("network unreachable")
            return addr

        with patch.object(security.socket, "create_connection", fake_connect):
            conn._create_socket(5)
        self.assertEqual(calls["tried"], [("2001:db8::1", 993), ("142.250.1.1", 993)])
        self.assertEqual(calls["server_hostname"], "imap.example.com")

    def test_imap_connect_checks_host_before_connecting(self):
        import imap_sync
        cred = imap_sync.Credential("a@b.com", "localhost", 993, "a@b.com", "pw")
        with patch.object(security.socket, "getaddrinfo", _addrinfo("127.0.0.1")), \
                patch.object(imap_sync, "PinnedIMAP4_SSL") as pinned:
            with self.assertRaises(security.UnsafeHost):
                imap_sync.connect(cred)
            pinned.assert_not_called()


class StartReportTests(unittest.TestCase):
    body = {"email": "me@school.ac.kr", "password": "pw", "provider": "custom", "host": "mail.school.ac.kr",
            "port": 993, "consented": True, "terms": True}

    def setUp(self):
        app.REPORT_IP_LIMIT.clear()
        app.REPORT_OWNER_LIMIT.clear()
        self.saved = dict(app._reports)
        self.client = TestClient(app.app)

    def tearDown(self):
        app._reports.clear()
        app._reports.update(self.saved)
        self.client.close()

    def test_internal_host_is_rejected_without_connecting(self):
        with patch.object(security.socket, "getaddrinfo", _addrinfo("169.254.169.254")), \
                patch("imap_sync.PinnedIMAP4_SSL") as pinned:
            response = self.client.post("/api/reports", json=self.body, headers=GUEST)
        self.assertEqual(response.status_code, 400)
        self.assertIn("993", response.json()["detail"])
        pinned.assert_not_called()

    def test_other_port_is_rejected(self):
        with patch("imap_sync.PinnedIMAP4_SSL") as pinned:
            response = self.client.post("/api/reports", json={**self.body, "port": 6379}, headers=GUEST)
        self.assertEqual(response.status_code, 400)
        pinned.assert_not_called()

    def test_bad_certificate_has_its_own_message(self):
        error = ssl.SSLCertVerificationError("certificate verify failed")
        with patch.object(app, "connect", side_effect=error):
            response = self.client.post("/api/reports", json=self.body, headers=GUEST)
        self.assertEqual(response.status_code, 502)
        self.assertIn("인증서", response.json()["detail"])

    def test_connection_attempts_are_limited_per_owner(self):
        failure = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        with patch.object(app, "connect", side_effect=failure):
            codes = [self.client.post("/api/reports", json=self.body, headers=GUEST).status_code
                     for _ in range(app.REPORT_OWNER_LIMIT.limit + 1)]
        self.assertEqual(codes[:-1], [401] * app.REPORT_OWNER_LIMIT.limit)
        self.assertEqual(codes[-1], 429)

    def test_connection_attempts_are_limited_per_ip_across_guests(self):
        failure = imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        with patch.object(app, "connect", side_effect=failure):
            codes = [self.client.post("/api/reports", json=self.body,
                                      headers={"X-IDly-Guest": f"{i:040d}", "X-Forwarded-For": "1.2.3.4, 9.9.9.9"}).status_code
                     for i in range(app.REPORT_IP_LIMIT.limit + 1)]
        self.assertEqual(codes[-1], 429)

    def test_one_running_analysis_per_owner(self):
        class Running:
            status = "받는 중"
            report = None

        running = app.Report("old@school.ac.kr", None, job=Running(), guest=app.hashlib.sha256(("g" * 40).encode()).hexdigest())
        app._reports[running.id] = running
        with patch.object(app, "connect") as connect:
            response = self.client.post("/api/reports", json=self.body, headers=GUEST)
        self.assertEqual(response.status_code, 429)
        connect.assert_not_called()

    def test_total_running_analyses_are_capped(self):
        class Running:
            status = "분석 중"
            report = None

        for i in range(app.MAX_ACTIVE_REPORTS):
            r = app.Report(f"u{i}@x.com", f"user-{i}", job=Running())
            app._reports[r.id] = r
        with patch.object(app, "connect") as connect:
            response = self.client.post("/api/reports", json=self.body, headers=GUEST)
        self.assertEqual(response.status_code, 429)
        connect.assert_not_called()


    def test_parallel_starts_by_same_owner_get_one_slot(self):
        import threading
        entered, release = threading.Event(), threading.Event()

        class Conn:
            def logout(self):
                pass

        def slow_connect(cred):
            entered.set()
            release.wait(5)
            return Conn()

        results = []
        with patch.object(app, "connect", slow_connect), patch.object(app, "start_sync") as sync:
            sync.return_value.status = "받는 중"
            sync.return_value.report = None
            first = threading.Thread(target=lambda: results.append(
                self.client.post("/api/reports", json=self.body, headers=GUEST).status_code))
            first.start()
            entered.wait(5)
            second = self.client.post("/api/reports", json=self.body, headers=GUEST).status_code
            release.set()
            first.join(5)
        self.assertEqual(second, 429)
        self.assertEqual(results, [200])
        self.assertEqual(app._starting, set())


class HeaderTests(unittest.TestCase):
    def test_security_headers_on_pages_and_api(self):
        client = TestClient(app.app)
        for path in ("/", "/api/config", "/legal/terms", "/static/app.js"):
            with self.subTest(path=path):
                headers = client.get(path).headers
                self.assertEqual(headers["x-frame-options"], "DENY")
                self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
                self.assertEqual(headers["x-content-type-options"], "nosniff")
                self.assertIn("referrer-policy", headers)
        client.close()

    def test_client_ip_uses_last_forwarded_entry(self):
        class Req:
            headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.7"}
            client = None

        self.assertEqual(security.client_ip(Req()), "203.0.113.7")


class SessionTests(unittest.TestCase):
    def test_session_token_is_stored_hashed(self):
        user = db.upsert_user("kakao", "uid-session-test", "s@example.com", "S")
        token = db.create_session(user["id"])
        stored = [r["token"] for r in db._all("SELECT token FROM sessions WHERE user_id = ?", (user["id"],))]
        self.assertNotIn(token, stored)
        self.assertEqual(len(stored[0]), 64)
        self.assertEqual(db.session_user(token)["id"], user["id"])
        db.delete_session(token)
        self.assertIsNone(db.session_user(token))


class SampleTests(unittest.TestCase):
    def test_sample_is_shared(self):
        client = TestClient(app.app)
        first = client.post("/api/reports/sample").json()["id"]
        second = client.post("/api/reports/sample").json()["id"]
        self.assertEqual(first, second)
        client.close()


class DefaultTests(unittest.TestCase):
    def test_debug_and_mock_login_are_off_by_default(self):
        env = {k: v for k, v in os.environ.items() if k not in ("IDLY_DEBUG", "AUTH_MODE", "DATABASE_URL")}
        code = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
                "import app, auth; print(app.DEBUG, auth.AUTH_MODE)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split()[-2:], ["False", "oauth"])


if __name__ == "__main__":
    unittest.main()
