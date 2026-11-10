import os
import unittest
from unittest.mock import patch

# .env의 실제 DB에 붙지 않게 app을 불러오기 전에 비운다
os.environ["DATABASE_URL"] = ""
os.environ["IDLY_DB"] = ":memory:"

from fastapi.testclient import TestClient  # noqa: E402

import app  # noqa: E402
import db  # noqa: E402

VALID = {"company": "테스트 회사", "name": "홍길동", "contact": "boss@example.com", "employees": "1~10명", "message": "문의합니다"}
SMTP_ENV = {"SMTP_USER": "sender@example.com", "SMTP_PASSWORD": "pw", "SMTP_PORT": "465"}


class FakeSmtp:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        pass

    def send_message(self, mail):
        self.sent.append(mail)


class OfficeInquiryTest(unittest.TestCase):
    def setUp(self):
        self.assertFalse(db.USE_POSTGRES)
        FakeSmtp.sent = []
        app._office_buckets.clear()
        self.client = TestClient(app.app)

    def post(self, **changes):
        return self.client.post("/api/office-inquiry", json={**VALID, **changes})

    def test_sends_to_contact_email_with_reply_to(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            self.assertEqual(self.post().status_code, 204)
        mail = FakeSmtp.sent[0]
        self.assertEqual(mail["To"], app.BUSINESS["email"])
        self.assertEqual(mail["Reply-To"], "boss@example.com")
        self.assertIn("테스트 회사", mail["Subject"])
        self.assertIn("문의합니다", mail.get_content())

    def test_phone_contact_has_no_reply_to(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            self.assertEqual(self.post(contact="010-1234-5678").status_code, 204)
        self.assertIsNone(FakeSmtp.sent[0]["Reply-To"])

    def test_newlines_cannot_add_mail_headers(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            self.assertEqual(self.post(company="회사\r\nBcc: evil@example.com").status_code, 204)
        self.assertIsNone(FakeSmtp.sent[0]["Bcc"])

    def test_missing_fields_are_rejected(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            self.assertEqual(self.post(company=" ").status_code, 400)
            self.assertEqual(self.post(employees="많음").status_code, 400)
        self.assertEqual(FakeSmtp.sent, [])

    def test_bot_field_is_dropped_silently(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            self.assertEqual(self.post(website="spam").status_code, 204)
        self.assertEqual(FakeSmtp.sent, [])

    def test_without_smtp_settings_user_sees_contact_email(self):
        with patch.dict(os.environ, {"SMTP_USER": "", "SMTP_PASSWORD": ""}):
            res = self.post()
        self.assertEqual(res.status_code, 503)
        self.assertIn(app.BUSINESS["email"], res.text)

    def test_rate_limit(self):
        with patch.dict(os.environ, SMTP_ENV), patch.object(app.smtplib, "SMTP_SSL", FakeSmtp):
            codes = [self.post().status_code for _ in range(6)]
        self.assertEqual(codes, [204] * 5 + [429])


if __name__ == "__main__":
    unittest.main()
