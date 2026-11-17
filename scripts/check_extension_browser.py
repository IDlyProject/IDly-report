"""격리된 Chromium + 로컬 HTTPS에서 실제 확장/CDP 모듈을 검증한다.

실제 계정·OpenAI·외부 사이트는 사용하지 않는다. 임시 프로필은 종료 후 정리한다.
실행: .venv/bin/python scripts/check_extension_browser.py
"""
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
import ssl
import tempfile
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith('/frame') or self.path.startswith('/oop'):
            label = 'OOP 클릭' if self.path.startswith('/oop') else 'iframe 클릭'
            body = f'<button onclick="window.clicks=(window.clicks||0)+1">{label}</button><label>{label} 입력<textarea id="frame-input"></textarea></label>'
        elif self.path.startswith('/login'):
            body = '<h1>로그인</h1><input type="password" value="PRIVATE_PASSWORD"><button>로그인</button>'
        elif self.path.startswith('/foreign'):
            body = '<h1>FOREIGN_PRIVATE_TEXT</h1><button>외부 프레임 버튼</button>'
        else:
            port = self.server.server_address[1]
            body = f'''<h1>계정 설정</h1>
              <label>탈퇴 사유<textarea id="reason">PRIVATE_TEXTAREA_VALUE</textarea></label>
              <button id="action" onclick="window.clicks=(window.clicks||0)+1"><span>회원 탈퇴</span></button>
              <a href="https://invalid.example/">외부 링크</a>
              <iframe title="same-site" src="/frame" width="400" height="90"></iframe>
              <iframe title="oop" src="https://a.localhost:{port}/oop" width="400" height="90"></iframe>
              <iframe title="excluded" src="https://127.0.0.1:{port}/foreign" width="400" height="90"></iframe>
              <div style="height:1500px">스크롤 내용</div>'''
        data = ('<!doctype html><html lang="ko"><meta charset="utf-8"><body>' + body + '</body></html>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_cert(folder):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.SubjectAlternativeName([
                x509.DNSName('localhost'), x509.DNSName('a.localhost'),
                x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False).sign(key, hashes.SHA256()))
    key_file, cert_file = folder / 'test-key.pem', folder / 'test-cert.pem'
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    key_file.chmod(0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_file, key_file


def main():
    with tempfile.TemporaryDirectory(prefix='idly-cdp-') as tmp:
        folder = Path(tmp)
        cert, key = make_cert(folder)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                extension = str(ROOT / 'extension')
                context = p.chromium.launch_persistent_context(str(folder / 'profile'), headless=True,
                    channel='chromium', ignore_https_errors=True, args=[
                        f'--disable-extensions-except={extension}', f'--load-extension={extension}',
                        '--ignore-certificate-errors', '--site-per-process', '--disable-background-networking',
                        '--disable-component-update', '--disable-sync', '--no-first-run'])
                try:
                    from urllib.parse import urlsplit
                    port = server.server_address[1]

                    def restrict(route):
                        u = urlsplit(route.request.url)
                        if u.hostname in ('localhost', 'a.localhost', '127.0.0.1') and u.port == port:
                            route.continue_()
                        else:
                            route.abort()

                    context.route('**/*', restrict)
                    worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                    page = context.new_page()
                    url = f'https://localhost:{port}/'
                    page.goto(url, wait_until='networkidle')
                    tab_id = worker.evaluate('async url => (await chrome.tabs.query({})).find(t=>t.url===url).id', url)
                    worker.evaluate('async tabId => {globalThis.fixtureSession=new IdlyBrowser.BrowserSession(chrome,tabId,"localhost");await fixtureSession.attach()}', tab_id)

                    def snapshot():
                        return worker.evaluate('async()=>await fixtureSession.snapshot()')

                    def action(method, element, s, value=None):
                        return worker.evaluate('async p=>await fixtureSession[p.method](p.ref,...(p.method==="fill"?[p.value]:[]),p.snapshotId)',
                            dict(method=method, ref=element['ref'], snapshotId=s['snapshotId'], value=value))

                    s = snapshot()
                    assert not s['password']
                    assert s['blockedFrames'] == 1, s
                    assert 'PRIVATE_TEXTAREA_VALUE' not in str(s), 'textarea leaked through AX children'
                    assert 'FOREIGN_PRIVATE_TEXT' not in str(s), 'foreign frame text leaked'
                    assert not any(e['text'] == '외부 링크' for e in s['elements'])
                    print('PASS: actual extension AX snapshot, input redaction, foreign frame/link exclusion', flush=True)

                    field = next(e for e in s['elements'] if e['text'] == '탈퇴 사유')
                    action('fill', field, s, '사용하지 않아요')
                    assert page.locator('#reason').input_value() == '사용하지 않아요'
                    print('PASS: actual CDP text input', flush=True)

                    s = snapshot()
                    button = next(e for e in s['elements'] if e['text'] == '회원 탈퇴')
                    action('click', button, s)
                    assert page.evaluate('window.clicks') == 1
                    print('PASS: actual CDP pointer click', flush=True)

                    for label in ('iframe 클릭', 'OOP 클릭'):
                        s = snapshot()
                        button = next(e for e in s['elements'] if e['text'] == label)
                        action('click', button, s)
                        frame = next(f for f in page.frames if ('/oop' if label == 'OOP 클릭' else '/frame') in f.url)
                        assert frame.evaluate('window.clicks') == 1, label
                        print('PASS: actual CDP ' + label, flush=True)
                        s = snapshot()
                        field = next(e for e in s['elements'] if e['text'] == label + ' 입력')
                        action('fill', field, s, '사용하지 않아요')
                        assert frame.locator('#frame-input').input_value() == '사용하지 않아요'
                        print('PASS: frame text input and ancestor focus chain: ' + label, flush=True)

                    for label in ('iframe 클릭', 'OOP 클릭'):
                        s = snapshot()
                        button = next(e for e in s['elements'] if e['text'] == label)
                        page.evaluate('''() => {const e=document.createElement('div');e.id='overlay';e.style='position:fixed;inset:0;z-index:2147483647;background:transparent';e.onclick=()=>window.overlayClicks=(window.overlayClicks||0)+1;document.body.append(e);}''')
                        rejected = worker.evaluate('async p=>{try{await fixtureSession.click(p.ref,p.snapshotId);return false}catch(e){return true}}',
                            dict(ref=button['ref'], snapshotId=s['snapshotId']))
                        assert rejected, 'ancestor overlay must block ' + label
                        assert page.evaluate('window.overlayClicks||0') == 0
                        frame = next(f for f in page.frames if ('/oop' if label == 'OOP 클릭' else '/frame') in f.url)
                        assert frame.evaluate('window.clicks') == 1
                        page.locator('#overlay').evaluate('(e)=>e.remove()')
                    print('PASS: ancestor overlays block both iframe click types', flush=True)

                    page.evaluate('''() => {
                      const wrapper=document.createElement('div');wrapper.style='transform:translate(12px,8px) scale(0.9)';
                      wrapper.innerHTML='<button id="transformed" onclick="window.transformedClicks=(window.transformedClicks||0)+1">변환 버튼</button>';
                      document.body.prepend(wrapper);
                    }''')
                    s = snapshot()
                    button = next(e for e in s['elements'] if e['text'] == '변환 버튼')
                    action('click', button, s)
                    assert page.evaluate('window.transformedClicks') == 1
                    print('PASS: transformed target click without blanket rejection', flush=True)

                    s = snapshot()
                    button = next(e for e in s['elements'] if e['text'] == '회원 탈퇴')
                    page.locator('#action').evaluate('(e)=>e.innerText="다른 동작"')
                    rejected = worker.evaluate('async p=>{try{await fixtureSession.click(p.ref,p.snapshotId);return false}catch(e){return e.name==="StaleSnapshotError"}}',
                        dict(ref=button['ref'], snapshotId=s['snapshotId']))
                    assert rejected and page.evaluate('window.clicks') == 1
                    print('PASS: stale target rejected before pointer dispatch', flush=True)

                    page.evaluate('''() => {
                      const host=document.createElement('div'); document.body.prepend(host);
                      host.id='shadow-fixture';
                      host.attachShadow({mode:'open'}).innerHTML='<button onclick="window.shadowClicks=(window.shadowClicks||0)+1">Shadow 클릭</button><label>Shadow 사유<textarea></textarea></label>';
                      const panel=document.createElement('div'); panel.id='idly-agent-panel'; document.body.prepend(panel);
                      panel.attachShadow({mode:'open'}).innerHTML='<p>PRIVATE_PANEL_STATUS</p><button>패널 버튼</button>';
                    }''')
                    s = snapshot()
                    assert 'PRIVATE_PANEL_STATUS' not in str(s) and '패널 버튼' not in str(s)
                    button = next(e for e in s['elements'] if e['text'] == 'Shadow 클릭')
                    action('click', button, s)
                    assert page.evaluate('window.shadowClicks') == 1
                    s = snapshot()
                    field = next(e for e in s['elements'] if e['text'] == 'Shadow 사유')
                    action('fill', field, s, '사용하지 않아요')
                    assert page.locator('#shadow-fixture textarea').input_value() == '사용하지 않아요'
                    print('PASS: shadow DOM click and own-panel text exclusion', flush=True)

                    page.evaluate('''() => {
                      const wrapper=document.createElement('div');wrapper.innerHTML='<label>포커스 원본<input id="focus-original"></label><label>다른 입력<input id="focus-decoy"></label>';
                      document.body.prepend(wrapper);
                      document.querySelector('#focus-original').onfocus=()=>queueMicrotask(()=>document.querySelector('#focus-decoy').focus());
                    }''')
                    s = snapshot()
                    field = next(e for e in s['elements'] if e['text'] == '포커스 원본')
                    rejected = worker.evaluate('async p=>{try{await fixtureSession.fill(p.ref,"입력 금지",p.snapshotId);return false}catch(e){return true}}',
                        dict(ref=field['ref'], snapshotId=s['snapshotId']))
                    assert rejected and page.locator('#focus-decoy').input_value() == ''
                    print('PASS: focus redirection cannot receive approved field text', flush=True)

                    page.evaluate('''() => {
                      const original=document.createElement('button');original.id='bridge-original';original.textContent='브리지 원본';
                      const decoy=document.createElement('button');decoy.id='bridge-decoy';decoy.textContent='다른 버튼';
                      decoy.onclick=()=>window.decoyClicks=(window.decoyClicks||0)+1;
                      document.body.prepend(original,decoy);
                      const observer=new MutationObserver(records=>{
                        for(const r of records) if(r.target===original&&r.attributeName.startsWith('data-idly-action-')) {
                          observer.disconnect();decoy.setAttribute(r.attributeName,'');original.removeAttribute(r.attributeName);break;
                        }
                      });observer.observe(original,{attributes:true});
                    }''')
                    s = snapshot()
                    button = next(e for e in s['elements'] if e['text'] == '브리지 원본')
                    rejected = worker.evaluate('async p=>{try{await fixtureSession.click(p.ref,p.snapshotId);return false}catch(e){return true}}',
                        dict(ref=button['ref'], snapshotId=s['snapshotId']))
                    assert rejected and page.evaluate('window.decoyClicks||0') == 0
                    print('PASS: page cannot redirect the element bridge to another node', flush=True)

                    page.evaluate('''() => {
                      const button=document.createElement('button');button.id='retry-action';button.textContent='대기 중 변경';
                      button.onclick=()=>window.retryClicks=(window.retryClicks||0)+1;document.body.prepend(button);
                    }''')
                    s = snapshot()
                    button = next(e for e in s['elements'] if e['text'] == '대기 중 변경')
                    worker.evaluate('''() => {
                      let fired=false;
                      fixtureSession.beforeAction=async()=>{
                        if(fired)return;fired=true;
                        const r=[...fixtureSession.refs.values()].find(r=>r.name==='대기 중 변경');
                        await fixtureSession._object(r, `function(){
                          const overlay=document.createElement('div');overlay.style='position:fixed;inset:0;z-index:2147483647';document.body.append(overlay);
                          setTimeout(()=>{this.textContent='변경된 조치';overlay.remove();},150);
                        }`);
                      };
                    }''')
                    rejected = worker.evaluate('async p=>{try{await fixtureSession.click(p.ref,p.snapshotId);return false}catch(e){return true}finally{fixtureSession.beforeAction=null}}',
                        dict(ref=button['ref'], snapshotId=s['snapshotId']))
                    assert rejected and page.evaluate('window.retryClicks||0') == 0
                    print('PASS: semantic mutation while action waits never executes', flush=True)

                    with page.expect_popup() as popup_info:
                        page.evaluate('() => window.open("/frame?popup=1")')
                    popup = popup_info.value
                    popup.wait_for_load_state()
                    detached = worker.evaluate('''async()=>{
                      const tab=(await chrome.tabs.query({})).find(t=>t.url?.includes('popup=1'));
                      try{await chrome.debugger.sendCommand({tabId:tab.id},'Page.getFrameTree');return false}catch{return true}
                    }''')
                    assert detached, 'unadopted popup must not remain attached'
                    popup.close()
                    print('PASS: unadopted popup does not acquire a debugger connection', flush=True)

                    page.goto(url + 'login', wait_until='networkidle')
                    s = snapshot()
                    assert s['password'] and not s['elements'] and not s['text']
                    assert 'PRIVATE_PASSWORD' not in str(s)
                    print('PASS: actual password page handoff without content', flush=True)
                    page.goto(url, wait_until='networkidle')
                    page.evaluate('''() => {const e=document.createElement('input');e.autocomplete='section-auth one-time-code';e.value='PRIVATE_OTP';document.body.prepend(e);}''')
                    s = snapshot()
                    assert s['password'] and not s['elements'] and not s['text']
                    print('PASS: section-prefixed OTP autocomplete handoff', flush=True)
                    worker.evaluate('async()=>await fixtureSession.close()')
                finally:
                    context.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
