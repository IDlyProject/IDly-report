"""실제 OpenAI·계정 사이트 호출 없이 확장 작업 계약과 접근 권한을 검증한다."""
import hashlib
import os
import secrets
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import app
from actions import extension_runs as runs


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"IDLY_EXTENSION": "1", "OPENAI_API_KEY": "test-only"})
        self.env.start()
        self.client = TestClient(app.app)
        self.token = secrets.token_urlsafe(32)
        self.headers = {app.GUEST_HEADER: self.token}
        self.report = app.Report("me@example.com", None, guest=hashlib.sha256(self.token.encode()).hexdigest(), data={
            "accounts": [{"id": "example.com", "service": "예시", "unused": True, "recommended": "탈퇴"}]})
        self.report.paid = True
        app._reports[self.report.id] = self.report
        self.path = f"/api/reports/{self.report.id}/extension-runs"
        runs._runs.clear()
        # 테스트가 모킹을 빠뜨려도 외부 호출을 실행할 수 없다.
        self.network = patch('actions.ai_agent.httpx.post', side_effect=AssertionError('실제 API 호출 금지'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.env.stop()
        self.client.close()
        app._reports.pop(self.report.id, None)
        runs._runs.clear()

    def start(self, **changes):
        return self.client.post(self.path, headers=self.headers, json={
            "account_id": "example.com", "action": "탈퇴", "consented": True, **changes})

    def test_start_commands_confirm_and_complete(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        launch = response.json()
        self.assertEqual(launch['startUrl'], 'https://example.com')
        self.assertEqual(launch['apiBase'], 'http://testserver')
        token = {"Authorization": "Bearer " + launch['token']}
        base = '/api/agent/' + launch['id']
        with patch.object(runs, '_ask', return_value={"decision": "click", "index": 0}):
            response = self.client.post(base + '/decide', headers=token, json={
                "url": 'https://example.com/account', "elements": [
                    {"i": 0, "tag": "a", "text": "계정 삭제", "href": "/delete"}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['confirm'])
        self.assertEqual(self.client.post(base + '/status', headers=token, json={
            "status": "확인 필요", "message": "확정할까요?"}).status_code, 200)
        listed = self.client.get(self.path, headers=self.headers).json()['runs']
        self.assertEqual(listed[0]['status'], '확인 필요')
        self.assertNotIn('token', listed[0])
        command = self.path + '/' + launch['id'] + '/commands'
        self.assertEqual(self.client.post(command, headers=self.headers, json={"type": "confirm"}).status_code, 200)
        self.assertEqual(self.client.get(base + '/commands', headers=token).json()['commands'], ['confirm'])
        self.assertEqual(self.client.get(base + '/commands', headers=token).json()['commands'], [])
        self.client.post(base + '/status', headers=token, json={"status": "완료"})
        self.assertEqual(self.client.get(base + '/commands', headers=token).status_code, 401)

    def test_owner_payment_sample_consent_and_feature_gate(self):
        self.assertEqual(self.client.post(self.path, json={"account_id": "example.com", "action": "탈퇴"}).status_code, 404)
        self.report.paid = False
        self.assertEqual(self.start().status_code, 402)
        self.report.paid = True
        self.report.data["sample"] = True
        self.assertEqual(self.start().status_code, 403)
        self.report.data["sample"] = False
        self.assertEqual(self.start(consented=False).status_code, 422)
        with patch.dict(os.environ, {"IDLY_EXTENSION": "0"}):
            self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.start(account_id="evil.com").status_code, 422)
        self.assertEqual(self.start(action="해지").status_code, 422)
        self.assertEqual(self.start(action="보안").status_code, 422)

    def test_duplicate_invalid_token_expiry_and_report_removal(self):
        launch = self.start().json()
        self.assertEqual(self.start().status_code, 409)
        base = '/api/agent/' + launch['id'] + '/commands'
        self.assertEqual(self.client.get(base).status_code, 401)
        self.assertEqual(self.client.get(base, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        token = {"Authorization": "Bearer " + launch['token']}
        runs.get(launch['id']).created = time.monotonic() - 3601
        self.assertEqual(self.client.get(base, headers=token).status_code, 401)
        runs.get(launch['id']).created = time.monotonic()
        self.report.paid = False
        self.assertEqual(self.client.get(base, headers=token).status_code, 401)
        self.report.paid = True
        app._reports.pop(self.report.id)
        self.assertEqual(self.client.get(base, headers=token).status_code, 401)

    def test_navigation_guards_and_invalid_ai_element(self):
        run = runs.ExtensionRun('report', 'example.com', '탈퇴', '예시', '')
        state = {"text": "", "elements": [{"i": 0, "text": "설정", "tag": "a", "href": "//evil.com/settings"}]}
        with patch.object(runs, '_ask', return_value={"decision": "click", "index": 0}) as ask:
            self.assertEqual(run.decide('test', 'https://evil.com/login', state, [])['decision'], 'need_login')
            ask.assert_not_called()
            self.assertEqual(run.decide('test', 'https://example.com', state, [])['decision'], 'skip')
        for target in ['https://evil.com', 'javascript:alert(1)', 'https://evil.com\\.example.com', 'http://example.com']:
            with self.subTest(target=target), patch.object(runs, '_ask', return_value={"decision": "goto", "url": target}):
                self.assertEqual(run.decide('test', 'https://example.com', state, [])['decision'], 'skip')
        with patch.object(runs, '_ask', return_value={"decision": "click", "index": True}):
            self.assertEqual(run.decide('test', 'https://example.com', state, [])['decision'], 'skip')

    def test_commands_cannot_access_another_report_run(self):
        other = runs.create(runs.ExtensionRun('other-report', 'example.com', '탈퇴', '예시', ''))
        response = self.client.post(self.path + '/' + other.id + '/commands', headers=self.headers, json={"type": "cancel"})
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
