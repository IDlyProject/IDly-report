const { test } = require('node:test');
const assert = require('node:assert/strict');
const { BrowserSession, StaleSnapshotError } = require('../extension/browser-session.js');

const ax = (id, role, name, props = []) => ({ nodeId: `ax${id}`, backendDOMNodeId: id,
  role: { value: role }, name: { value: name }, properties: props });

function harness() {
  const listeners = new Set(), detach = new Set();
  const state = {
    url: 'https://example.com/account?secret=not-for-ai#token', loaderId: 'doc1', password: false,
    frames: [], nodes: [ax(1, 'heading', '계정 설정'), ax(2, 'button', '회원 탈퇴'), ax(3, 'textbox', '탈퇴 사유'),
      { ...ax(4, 'textbox', '일반 메모'), value: { value: 'PRIVATE_VALUE' } }],
    dom: new Map([[2, { nodeName: 'BUTTON', attributes: [] }],
      [3, { nodeName: 'TEXTAREA', attributes: [] }], [4, { nodeName: 'INPUT', attributes: ['type', 'text'] }]]),
    partial: new Map(), clicks: [], filled: [], scrolled: [], navigated: [], detached: [], commands: [],
  };
  const chrome = { debugger: {
    onEvent: { addListener: f => listeners.add(f), removeListener: f => listeners.delete(f) },
    onDetach: { addListener: f => detach.add(f), removeListener: f => detach.delete(f) },
    attach: async () => {}, detach: async target => state.detached.push(target),
    sendCommand: async (target, method, params = {}) => {
      state.commands.push({ target, method, params });
      if (method === 'Page.getFrameTree') return { frameTree: { frame: {
        id: 'main', url: state.url, loaderId: state.loaderId }, childFrames: state.frames } };
      if (method === 'Page.createIsolatedWorld') return { executionContextId: params.frameId === 'main' ? 1 : 2 };
      if (method === 'Runtime.evaluate') return { result: { value: { password: state.password, panel: false } } };
      if (method === 'Accessibility.getFullAXTree') return { nodes: params.frameId === 'main' ? state.nodes : [ax(20, 'button', '프레임 버튼')] };
      if (method === 'Accessibility.getPartialAXTree') {
        const node = state.partial.get(params.backendNodeId) || state.nodes.find(n => n.backendDOMNodeId === params.backendNodeId) || ax(20, 'button', '프레임 버튼');
        return { nodes: [node] };
      }
      if (method === 'DOM.describeNode') return { node: { backendNodeId: params.backendNodeId,
        ...(state.dom.get(params.backendNodeId) || { nodeName: 'BUTTON', attributes: [] }) } };
      if (method === 'DOM.resolveNode') return { object: { objectId: String(params.backendNodeId) } };
      if (method === 'DOM.getFrameOwner') return { backendNodeId: 50 };
      if (method === 'Runtime.callFunctionOn') {
        if (params.functionDeclaration.includes('return {ok:h===this||')) return { result: { value: {ok: true, x: 50, y: 20} } };
        if (params.functionDeclaration.includes('return {ok:h===this')) return { result: { value: {ok: !state.overlay, x: 50, y: 20} } };
        if (params.functionDeclaration.includes('return {x:r.x')) return { result: { value: {x: 50, y: 20} } };
        if (params.functionDeclaration.includes('select()')) return { result: { value: true } };
        if (params.functionDeclaration.includes('getRootNode')) return { result: { value: false } };
        return { result: { value: true } };
      }
      if (method === 'DOM.getContentQuads') return { quads: [[0, 0, 100, 0, 100, 40, 0, 40]] };
      if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') state.clicks.push(target);
      if (method === 'Input.insertText') state.filled.push(params.text);
      if (method === 'Input.synthesizeScrollGesture') state.scrolled.push(params.yDistance);
      if (method === 'Page.getLayoutMetrics') return { cssVisualViewport: { clientWidth: 1000, clientHeight: 800 } };
      if (method === 'Page.navigate') state.navigated.push(params.url);
      return {};
    },
  } };
  const session = new BrowserSession(chrome, 7, 'example.com', { engine: 'cdp' });
  return { session, state, emit: (method, params, source = { tabId: 7 }) => [...listeners].forEach(f => f(source, method, params)),
    detach: () => [...detach].forEach(f => f({ tabId: 7 }, 'canceled_by_user')) };
}

test('AX snapshot returns role/name refs, strips URL secrets and excludes input values', async () => {
  const { session, state } = harness();
  await session.attach();
  const s = await session.snapshot();
  assert.equal(s.url, 'https://example.com/account');
  assert.equal(s.password, false);
  assert.equal(s.elements.length, 3);
  assert.equal(s.elements[0].role, 'button');
  assert.equal(s.elements[0].text, '회원 탈퇴');
  assert.ok(s.elements[0].ref);
  assert.equal(s.elements[1].editable, true);
  assert.equal(s.text, '계정 설정');
  assert.ok(!JSON.stringify(s).includes('PRIVATE_VALUE'));
  assert.ok(!JSON.stringify(s).includes('not-for-ai'));
  await session.click(s.elements[0].ref, s.snapshotId);
  assert.equal(state.clicks.length, 1);
  await session.close();
  assert.equal(state.detached.length, 1);
});

test('navigation and snapshot replacement invalidate original references', async () => {
  const { session, state, emit } = harness();
  await session.attach();
  const s = await session.snapshot();
  state.loaderId = 'doc2';
  emit('Page.frameNavigated', { frame: { id: 'main', url: state.url } });
  await assert.rejects(session.click(s.elements[0].ref, s.snapshotId), StaleSnapshotError);
  const next = await session.snapshot();
  await session.snapshot();
  await assert.rejects(session.click(next.elements[0].ref, next.snapshotId), StaleSnapshotError);
  assert.equal(state.clicks.length, 0);
});

test('renamed, disabled, detached and cross-site targets never receive a click', async () => {
  for (const mutate of [
    h => h.state.partial.set(2, ax(2, 'button', '다른 동작')),
    h => h.state.partial.set(2, ax(2, 'button', '회원 탈퇴', [{ name: 'disabled', value: { value: true } }])),
    h => { h.state.url = 'https://evil.com'; },
    h => h.detach(),
  ]) {
    const h = harness();
    await h.session.attach();
    const s = await h.session.snapshot();
    mutate(h);
    await assert.rejects(h.session.click(s.elements[0].ref, s.snapshotId));
    assert.equal(h.state.clicks.length, 0);
  }
});

test('safe text can be filled; changing field to password or OTP invalidates fill', async () => {
  for (const attrs of [[], ['type', 'password'], ['autocomplete', 'one-time-code'], ['autocomplete', 'cc-number'], ['autocomplete', 'section-auth one-time-code'], ['autocomplete', 'section-checkout cc-number']]) {
    const { session, state } = harness();
    await session.attach();
    const s = await session.snapshot();
    state.dom.set(3, { nodeName: 'TEXTAREA', attributes: attrs });
    if (!attrs.length) {
      await session.fill(s.elements[1].ref, '사용하지 않아요', s.snapshotId);
      assert.deepEqual(state.filled, ['사용하지 않아요']);
    } else {
      await assert.rejects(session.fill(s.elements[1].ref, 'do not send', s.snapshotId));
      assert.deepEqual(state.filled, []);
    }
  }
});

test('authentication screens expose no content or actionable elements', async () => {
  const { session, state } = harness();
  await session.attach();
  state.password = true;
  const s = await session.snapshot();
  assert.equal(s.password, true);
  assert.equal(s.text, '');
  assert.deepEqual(s.elements, []);
  assert.ok(!state.commands.some(c => c.method === 'Accessibility.getFullAXTree'));
});

test('new browser sessions cannot reuse snapshot identifiers and detach stops observation', async () => {
  const a = harness(), b = harness();
  await a.session.attach(); await b.session.attach();
  assert.notEqual((await a.session.snapshot()).snapshotId, (await b.session.snapshot()).snapshotId);
  a.detach();
  await assert.rejects(a.session.snapshot(), error => error.name !== 'StaleSnapshotError');
});

test('editable descendants do not leak textarea contents through StaticText nodes', async () => {
  const { session, state } = harness();
  state.nodes[2].childIds = ['value-child'];
  state.nodes.push({ nodeId: 'value-child', backendDOMNodeId: 30,
    role: { value: 'StaticText' }, name: { value: 'PRIVATE_TEXTAREA_VALUE' } });
  await session.attach();
  const s = await session.snapshot();
  assert.ok(!JSON.stringify(s).includes('PRIVATE_TEXTAREA_VALUE'));
});

test('same-site iframe nodes have independent frame refs; cross-site frames are excluded', async () => {
  const { session, state } = harness();
  state.frames = [
    { frame: { id: 'child', parentId: 'main', url: 'https://example.com/frame', loaderId: 'frame1' } },
    { frame: { id: 'external', parentId: 'main', url: 'https://ads.invalid', loaderId: 'frame2' } },
  ];
  await session.attach();
  const s = await session.snapshot();
  const el = s.elements.find(e => e.text === '프레임 버튼');
  assert.ok(el);
  assert.equal(el.frameId, 'child');
  assert.equal(s.blockedFrames, 1);
  assert.ok(!state.commands.some(c => c.method === 'Accessibility.getFullAXTree' && c.params.frameId === 'external'));
  await session.click(el.ref, s.snapshotId);
  assert.equal(state.clicks.length, 1);
  const next = await session.snapshot();
  state.overlay = true;
  await assert.rejects(session.click(next.elements.find(e => e.frameId === 'child').ref, next.snapshotId));
  assert.equal(state.clicks.length, 1);
});

test('scroll is bounded to the attached page; goto rejects foreign and credential URLs', async () => {
  const { session, state } = harness();
  await session.attach();
  await session.scroll('down');
  assert.ok(state.scrolled[0] < 0);
  for (const url of ['https://evil.com', 'http://example.com', 'https://user:pass@example.com']) {
    await assert.rejects(session.goto(url));
  }
  await session.goto('https://example.com/settings');
  assert.deepEqual(state.navigated, ['https://example.com/settings']);
});

test('cross-site link is never a writable target even if requested directly', async () => {
  const { session, state } = harness();
  state.nodes = [ax(2, 'link', '설정')];
  state.dom.set(2, { nodeName: 'A', attributes: ['href', 'https://evil.com/settings'] });
  await session.attach();
  const s = await session.snapshot();
  assert.equal(s.elements.length, 0);
});
