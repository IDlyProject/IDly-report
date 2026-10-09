import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

import app
import db


class FakeDiscordClient:
    calls = []
    status = 204

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return httpx.Response(self.status, request=httpx.Request("POST", url))


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        app._feedback_buckets.clear()
        FakeDiscordClient.calls = []
        FakeDiscordClient.status = 204
        self.client = TestClient(app.app)
        self.env = patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.test/webhook"})
        self.http = patch.object(app.httpx, "AsyncClient", FakeDiscordClient)
        self.env.start()
        self.http.start()

    def tearDown(self):
        self.http.stop()
        self.env.stop()
        self.client.close()

    def test_anonymous_message_has_same_discord_embed_fields(self):
        response = self.client.post("/api/feedback", data={"message": "  버튼이 눌리지 않아요  ", "screenPath": "#home"})
        self.assertEqual(response.status_code, 204)
        _, request = FakeDiscordClient.calls[0]
        payload = request["json"]
        self.assertEqual(payload["username"], "IDly 에러 제보")
        self.assertEqual(payload["embeds"][0]["title"], "🐛 버그 / 불편사항 제보")
        self.assertEqual(payload["embeds"][0]["fields"], [
            {"name": "내용", "value": "버튼이 눌리지 않아요"},
            {"name": "화면", "value": "리포트 · 홈", "inline": True},
            {"name": "유저", "value": "익명", "inline": True},
        ])

    def test_images_use_discord_attachments_and_signed_in_email(self):
        user = db.upsert_user("dev", "feedback-test", "feedback-test@example.com", "테스트")
        self.client.cookies.set(app.SESSION_COOKIE, db.create_session(user["id"]))
        files = [("images", ("capture.png", b"\x89PNG\r\n\x1a\n", "image/png"))]
        response = self.client.post("/api/feedback", data={"message": "이미지 첨부 테스트", "screenPath": "#r=private-id"}, files=files)
        self.assertEqual(response.status_code, 204)
        _, request = FakeDiscordClient.calls[0]
        payload = json.loads(request["data"]["payload_json"])
        self.assertEqual(payload["embeds"][0]["fields"][1]["value"], "리포트 · 리포트")
        self.assertEqual(payload["embeds"][0]["fields"][2]["value"], "feedback-test@example.com")
        self.assertEqual(payload["embeds"][0]["image"]["url"], "attachment://feedback_0.png")
        self.assertEqual(request["files"]["files[0]"][1], b"\x89PNG\r\n\x1a\n")

    def test_validation_and_rate_limit(self):
        self.assertEqual(self.client.post("/api/feedback", data={"message": "짧음"}).status_code, 400)
        files = [("images", (f"{i}.png", b"image", "image/png")) for i in range(6)]
        self.assertEqual(self.client.post("/api/feedback", data={"message": "사진이 너무 많아요"}, files=files).status_code, 400)
        for _ in range(3):
            self.assertEqual(self.client.post("/api/feedback", data={"message": "제보 횟수 테스트"}).status_code, 204)
        self.assertEqual(self.client.post("/api/feedback", data={"message": "네 번째 제보"}).status_code, 429)
        self.assertEqual(len(FakeDiscordClient.calls), 3)

    def test_discord_failure_is_not_reported_as_success(self):
        FakeDiscordClient.status = 400
        response = self.client.post("/api/feedback", data={"message": "전송 실패 테스트"})
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
