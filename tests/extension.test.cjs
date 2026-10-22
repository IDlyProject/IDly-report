const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const root = path.join(__dirname, '..');
const source = (file) => fs.readFileSync(path.join(root, file), 'utf8');
const manifest = JSON.parse(source('extension/manifest.json'));

test('bridge announces installation before documentElement is available and checks message origin', () => {
  let onMessage, onReady;
  const messages = [], forwarded = [];
  const document = { documentElement: null, addEventListener: (type, fn) => { onReady = fn; } };
  const window = { postMessage: (msg) => messages.push(msg), addEventListener: (type, fn) => { onMessage = fn; } };
  vm.runInNewContext(source('extension/bridge.js'), {
    chrome: { runtime: { getManifest: () => manifest, sendMessage: (msg) => forwarded.push(msg) } },
    location: { origin: 'https://idly.kr' }, document, window,
  });
  document.documentElement = { dataset: {} };
  onReady();
  assert.equal(document.documentElement.dataset.idlyExtension, manifest.version);
  assert.equal(messages.at(-1).type, 'READY');
  onMessage({ source: window, origin: 'https://evil.com', data: { source: 'idly-web', type: 'START' } });
  assert.equal(forwarded.length, 0);
  onMessage({ source: window, origin: 'https://idly.kr', data: { source: 'idly-web', type: 'START', run: { id: 'run' } } });
  assert.equal(forwarded[0].type, 'IDLY_START');
});

test('worker runs login → confirmation → click → completion without sending login-page contents', async () => {
  let onMessage;
  const statuses = [], decisions = [], requests = [];
  let clicks = 0, created = 0, loginPage = true;
  const chrome = {
    runtime: { getManifest: () => manifest, onMessage: { addListener: (fn) => { onMessage = fn; } } },
    tabs: {
      onCreated: { addListener() {} }, onRemoved: { addListener() {} },
      create: async () => { created++; return { id: 7 }; },
      get: async () => ({ status: 'complete' }), update: async () => ({ windowId: 1 }),
    },
    windows: { update: async () => {} },
    scripting: { executeScript: async ({ args }) => {
      if (args.length === 0) return [{ result: {
        url: 'https://example.com/account', password: loginPage,
        text: loginPage ? '로그인 정보' : '설정', elements: [{ i: 0, tag: 'button', text: '탈퇴' }],
      } }];
      if (args.length === 1) { clicks++; return [{ result: true }]; }
      return [{ result: undefined }];
    } },
  };
  let finished;
  const done = new Promise((resolve) => { finished = resolve; });
  vm.runInNewContext(source('extension/background.js'), {
    chrome, URL, setTimeout: (fn) => { queueMicrotask(fn); },
    fetch: async (url, options) => {
      const body = options.body ? JSON.parse(options.body) : null;
      requests.push({ url, body });
      let data = {};
      if (url.endsWith('/status')) {
        statuses.push(body.status);
        if (body.status === '완료') finished();
      } else if (url.endsWith('/commands')) {
        if (statuses.at(-1) === '입력 필요') { loginPage = false; data = { commands: ['resume'] }; }
        else data = { commands: statuses.at(-1) === '확인 필요' ? ['confirm'] : [] };
      } else if (url.endsWith('/decide')) {
        decisions.push(body);
        data = decisions.length === 1
          ? { decision: 'click', index: 0, label: '탈퇴', confirm: true }
          : { decision: 'done' };
      }
      return { ok: true, json: async () => data };
    },
  });
  const run = { id: 'run', apiBase: 'https://idly.kr', token: 'run-token', startUrl: 'https://example.com', service: '예시', action: '탈퇴' };
  onMessage({ type: 'IDLY_START', run }, { url: 'https://evil.com' });
  onMessage({ type: 'IDLY_START', run: { ...run, apiBase: 'https://evil.com' } }, { url: 'https://idly.kr' });
  assert.equal(created, 0);
  onMessage({ type: 'IDLY_START', run }, { url: 'https://idly.kr' });
  await Promise.race([done, new Promise((_, reject) => setTimeout(() => reject(new Error('worker timeout')), 2000))]);
  assert.equal(created, 1);
  assert.equal(clicks, 1);
  assert.ok(statuses.includes('입력 필요'));
  assert.ok(statuses.includes('확인 필요'));
  assert.equal(statuses.at(-1), '완료');
  assert.equal(decisions.length, 2);
  assert.ok(requests.every(({ body }) => !JSON.stringify(body).includes('로그인 정보')));
});

test('report UI offers extension start only when enabled and sends the consented account action', async () => {
  const nodes = new Map();
  function node() {
    return { children: [], hidden: false, disabled: false, classList: { add() {} },
      append(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    };
  }
  const body = node(), report = node();
  nodes.set('#report-body', body); nodes.set('#report', report);
  nodes.set('#extension-hint', node()); nodes.set('#extension-error', node()); nodes.set('#extension-runs', node());
  const link = { dataset: { accountId: 'example.com', extensionAction: '탈퇴' }, after(child) { this.start = child; } };
  const messages = [], calls = [];
  const window = { confirm: () => true, addEventListener() {}, postMessage: (msg) => messages.push(msg) };
  vm.runInNewContext(source('static/extension.js'), {
    window, location: { origin: 'https://idly.kr' }, clearTimeout() {}, setTimeout() {},
    document: {
      documentElement: { dataset: { idlyExtension: '0.1.0' } },
      createElement: node, querySelector: (sel) => nodes.get(sel),
      querySelectorAll: (sel) => sel.includes('data-extension-action') ? [link] : link.start ? [link.start] : [],
    },
    showError() {}, api: async (url, options) => {
      calls.push({ url, options });
      return options ? { id: 'run', token: 'token' } : { runs: [] };
    },
  });
  window.mountExtension('sample', false);
  assert.equal(body.children.length, 0);
  window.mountExtension('report', true);
  assert.equal(body.children.length, 1);
  assert.equal(link.start.disabled, false);
  await link.start.onclick();
  assert.equal(calls[0].url, '/api/reports/report/extension-runs');
  assert.deepEqual(JSON.parse(calls[0].options.body), { account_id: 'example.com', action: '탈퇴', consented: true });
  assert.equal(messages.at(-1).type, 'START');
  assert.equal(messages.at(-1).run.id, 'run');
});
