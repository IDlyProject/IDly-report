// 브라우저 제어 전용 모듈. 모델이 만든 JS/CDP 명령을 실행하지 않는다.
(() => {
  let sessionSequence = 0;
  const ACTION_ROLES = new Set(['button', 'link', 'menuitem', 'tab', 'checkbox', 'radio', 'textbox', 'searchbox', 'combobox']);
  const TEXT_ROLES = new Set(['heading', 'StaticText', 'alert', 'paragraph']);
  const AUTH_FIELD = /password|passwd|비밀번호|인증\s?(번호|코드)|verification|one.?time|otp|credit.?card|카드\s?번호/i;
  const fieldAttributes = (node) => Object.fromEntries((node.attributes || []).reduce((pairs, value, i, a) => {
    if (i % 2 === 0) pairs.push([value.toLowerCase(), a[i + 1]]);
    return pairs;
  }, []));
  const prop = (node, name) => node.properties?.find(p => p.name === name)?.value?.value;
  const publicURL = (raw) => { const u = new URL(raw); return `${u.origin}${u.pathname}`; };

  class StaleSnapshotError extends Error {
    constructor(message = '화면이 바뀌었어요. 다시 확인할게요.') { super(message); this.name = 'StaleSnapshotError'; }
  }

  const INSPECT_AUTH = `(() => {
    const roots = [document];
    for (let i = 0; i < roots.length; i++) {
      for (const e of roots[i].querySelectorAll('*')) if (e.shadowRoot) roots.push(e.shadowRoot);
    }
    const visible = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
    let password = false;
    for (const root of roots) for (const e of root.querySelectorAll('input')) {
      if (!visible(e)) continue;
      const type=(e.getAttribute('type')||'text').toLowerCase();
      const autocomplete=(e.getAttribute('autocomplete')||'').toLowerCase().split(/\\s+/);
      const label=[e.name,e.id,e.getAttribute('aria-label'),e.getAttribute('placeholder')].join(' ');
      const auth=/password|passwd|otp|one.?time|verification|비밀번호|인증.?번호|인증.?코드/i.test(label);
      const loginForm=e.form && /로그인|sign.?in|log.?in/i.test(e.form.innerText||'');
      if(type==='password'||autocomplete.some(t=>t==='one-time-code'||t==='username'||t==='current-password'||t==='new-password'||t.startsWith('cc-'))||auth||(['text','email','tel'].includes(type)&&loginForm)) password=true;
    }
    return {password};
  })()`;

  class BrowserSession {
    constructor(chromeAPI, tabId, domain, { engine = 'crx', beforeAction = null } = {}) {
      if (!['crx', 'cdp'].includes(engine)) throw new Error('지원하지 않는 브라우저 엔진이에요.');
      this.chrome = chromeAPI;
      this.tabId = tabId;
      this.identity = `${tabId}:${++sessionSequence}`;
      this.useCrx = engine === 'crx';
      this.beforeAction = beforeAction;
      this.driver = this.useCrx ? globalThis.IdlyCRX?.createDriver(tabId) : null;
      this.domain = String(domain).toLowerCase();
      this.attached = false;
      this.generation = 0;
      this.sequence = 0;
      this.current = null;
      this.refs = new Map();
      this.targets = new Map([['', {}]]);
      this.pending = new Set();
      this.connectionError = null;
      this.onEvent = this._event.bind(this);
      this.onDetach = (source) => {
        if (source.tabId === this.tabId) { this.attached = false; this._invalidate(); }
      };
    }

    allows(raw) {
      try {
        const u = new URL(raw);
        return u.protocol === 'https:' && !u.username && !u.password &&
          (u.hostname === this.domain || u.hostname.endsWith(`.${this.domain}`));
      } catch { return false; }
    }

    _invalidate() { this.generation++; this.current = null; this.refs.clear(); }
    _target(sessionId = '') { return sessionId ? { tabId: this.tabId, sessionId } : { tabId: this.tabId }; }
    _send(sessionId, method, params = {}) {
      if (!this.attached) throw new Error('브라우저 연결이 해제됐어요. 작업을 다시 시작해주세요.');
      return this.chrome.debugger.sendCommand(this._target(sessionId), method, params);
    }

    async attach() {
      this.chrome.debugger.onEvent.addListener(this.onEvent);
      this.chrome.debugger.onDetach.addListener(this.onDetach);
      try {
        this.attached = true;
        if (this.driver) await this.driver.attach();
        else if (!this.useCrx) await this.chrome.debugger.attach({ tabId: this.tabId }, '1.3');
        else throw new Error('확장 빌드가 필요해요. npm run build:extension 후 확장을 다시 로드해주세요.');
        await this._initialize('');
        await this._settled();
      } catch (error) {
        await this.close();
        throw new Error(`작업 탭에 연결하지 못했어요. 확장 권한과 다른 디버거 연결을 확인해주세요. (${error.message})`);
      }
    }

    async _initialize(sessionId) {
      await this._send(sessionId, 'Page.enable');
      await this._send(sessionId, 'DOM.enable');
      if (this.driver) return; // Playwright owns auto-attach and the child target sessions.
      await this._send(sessionId, 'Target.setAutoAttach', {
        autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
        filter: [{ type: 'iframe', exclude: false }],
      });
    }

    _event(source, method, params = {}) {
      if (source.tabId !== this.tabId) return;
      if (method === 'Target.attachedToTarget' && params.targetInfo?.type === 'iframe') {
        this._invalidate();
        this.targets.set(params.sessionId, params.targetInfo);
        if (this.driver) return; // Do not race Playwright's child-session initialization.
        const job = this._initialize(params.sessionId).catch(() => {
          this.connectionError = '프레임에 연결하지 못했어요. 다시 확인해주세요.';
        }).finally(() => this.pending.delete(job));
        this.pending.add(job);
      } else if (method === 'Target.detachedFromTarget') {
        this.targets.delete(params.sessionId);
        this._invalidate();
      } else if (['Page.frameNavigated', 'Page.frameDetached', 'DOM.documentUpdated'].includes(method)) {
        this._invalidate();
      }
    }

    async _settled() {
      while (this.pending.size) await Promise.all([...this.pending]);
      if (this.connectionError) throw new StaleSnapshotError(this.connectionError);
    }

    async _frames() {
      await this._settled();
      const frames = new Map();
      for (const sessionId of this.targets.keys()) {
        const { frameTree } = await this._send(sessionId, 'Page.getFrameTree');
        const visit = tree => {
          if (!tree?.frame) return;
          const f = { ...tree.frame, parentId: tree.frame.parentId || frames.get(tree.frame.id)?.parentId, sessionId };
          if (!frames.has(f.id) || sessionId) frames.set(f.id, f);
          for (const child of tree.childFrames || []) visit(child);
        };
        visit(frameTree);
      }
      if (frames.size > 32) throw new Error('프레임이 너무 많아 직접 확인이 필요해요.');
      return frames;
    }

    async _inspect(frame) {
      const { executionContextId } = await this._send(frame.sessionId, 'Page.createIsolatedWorld', {
        frameId: frame.id, worldName: 'idly-agent', grantUniveralAccess: false,
      });
      frame.contextId = executionContextId;
      const inspected = await this._send(frame.sessionId, 'Runtime.evaluate', {
        expression: INSPECT_AUTH, contextId: executionContextId, returnByValue: true,
      });
      if (inspected.exceptionDetails || !inspected.result?.value) throw new StaleSnapshotError();
      return inspected.result.value;
    }

    async _node(record) {
      const { node } = await this._send(record.sessionId, 'DOM.describeNode', { backendNodeId: record.backendNodeId });
      const attrs = fieldAttributes(node);
      const tag = node.nodeName.toLowerCase();
      const autocomplete = (attrs.autocomplete || '').toLowerCase().split(/\s+/);
      const type = (attrs.type || 'text').toLowerCase();
      const sensitive = type === 'password' || autocomplete.some(t => ['one-time-code', 'username', 'current-password', 'new-password'].includes(t) || t.startsWith('cc-')) ||
        AUTH_FIELD.test([attrs.name, attrs.id, attrs['aria-label'], record.name].join(' '));
      const editable = !sensitive && !('readonly' in attrs) && !('disabled' in attrs) &&
        (tag === 'textarea' || (tag === 'input' && ['text', 'search'].includes(type)));
      let href = '';
      if (attrs.href) {
        const url = new URL(attrs.href, record.url).href;
        if (!this.allows(url)) return { excluded: true };
        href = url;
      }
      return { tag, href, sensitive, editable, type, disabled: 'disabled' in attrs, readonly: 'readonly' in attrs };
    }

    async _object(record, declaration, args = []) {
      const { object } = await this._send(record.sessionId, 'DOM.resolveNode', {
        backendNodeId: record.backendNodeId, executionContextId: record.contextId,
      });
      try {
        const result = await this._send(record.sessionId, 'Runtime.callFunctionOn', {
          objectId: object.objectId, functionDeclaration: declaration, returnByValue: true,
          arguments: args.map(value => ({ value })),
        });
        if (result.exceptionDetails) throw new StaleSnapshotError();
        return result.result?.value;
      } finally {
        await this._send(record.sessionId, 'Runtime.releaseObject', { objectId: object.objectId }).catch(() => {});
      }
    }

    async snapshot() {
      this._invalidate();
      const frames = await this._frames();
      const root = [...frames.values()].find(f => !f.parentId && !f.sessionId);
      if (!root) throw new StaleSnapshotError();
      const url = publicURL(root.url);
      const allowed = [...frames.values()].filter(f => this.allows(f.url));
      const blocked = [...frames.values()].filter(f => !this.allows(f.url));
      const empty = { url, password: true, text: '', elements: [], snapshotId: '', blockedFrames: blocked.length, truncated: false };
      if (!this.allows(root.url)) return empty;
      // 먼저 모든 읽을 수 있는 프레임의 인증 입력을 검사한다. AX value는 읽거나 전송하지 않는다.
      for (const frame of allowed) if ((await this._inspect(frame)).password) return empty;
      if (blocked.some(f => /login|signin|oauth|captcha|challenge/i.test(f.url))) return empty;
      const generation = this.generation;
      const snapshotId = `${this.identity}:${generation}:${++this.sequence}`;
      const elements = [], texts = [], refs = new Map();
      let truncated = false;
      for (const frame of allowed) {
        const { nodes = [] } = await this._send(frame.sessionId, 'Accessibility.getFullAXTree', { frameId: frame.id });
        // AX value를 버려도 textarea의 값이 자식 StaticText로 나타날 수 있다.
        const byId = new Map(nodes.map(node => [node.nodeId, node]));
        const excluded = new Set();
        const excludeChildren = node => {
          for (const id of node.childIds || []) {
            if (excluded.has(id)) continue;
            excluded.add(id);
            if (byId.has(id)) excludeChildren(byId.get(id));
          }
        };
        for (const node of nodes) {
          if (['textbox', 'searchbox', 'combobox'].includes(node.role?.value) || prop(node, 'editable')) excludeChildren(node);
        }
        for (const node of nodes) {
          if (node.ignored || excluded.has(node.nodeId)) continue;
          const role = node.role?.value || '';
          const name = String(node.name?.value || '').trim();
          if (!node.backendDOMNodeId || !name) continue;
          if (!TEXT_ROLES.has(role) && !ACTION_ROLES.has(role)) continue;
          const record = { sessionId: frame.sessionId, frameId: frame.id, contextId: frame.contextId,
            loaderId: frame.loaderId, url: frame.url, backendNodeId: node.backendDOMNodeId,
            role, name, disabled: prop(node, 'disabled') === true, generation };
          const ownPanel = await this._object(record, `function(){let n=this;while(n){if(n.id==='idly-agent-panel')return true;n=n.parentElement||n.getRootNode?.().host;}return false;}`);
          if (ownPanel) continue;
          if (TEXT_ROLES.has(role)) texts.push(name);
          if (!ACTION_ROLES.has(role)) continue;
          const metadata = await this._node(record);
          if (metadata.sensitive) return empty;
          if (metadata.excluded) continue;
          if (elements.length >= 150) { truncated = true; continue; }
          Object.assign(record, metadata, { disabled: record.disabled || metadata.disabled });
          const ref = `${snapshotId}:e${elements.length}`;
          refs.set(ref, record);
          elements.push({ i: elements.length, ref, tag: metadata.tag, role, text: name.slice(0, 80),
            href: metadata.href ? publicURL(metadata.href) : '', frameId: frame.id,
            disabled: record.disabled, editable: metadata.editable });
        }
      }
      if (!this.attached || this.generation !== generation) throw new StaleSnapshotError();
      this.refs = refs;
      this.current = { snapshotId, generation };
      const allText = texts.join('\n');
      return { url, password: false, text: allText.slice(0, 2000), elements, snapshotId,
        blockedFrames: blocked.length, truncated: truncated || allText.length > 2000 };
    }

    _assertSnapshot(snapshotId) {
      if (!this.attached || !this.current || this.current.snapshotId !== snapshotId ||
          this.current.generation !== this.generation) throw new StaleSnapshotError();
    }

    async _validate(ref, snapshotId) {
      this._assertSnapshot(snapshotId);
      const record = this.refs.get(ref);
      if (!record || record.disabled) throw new StaleSnapshotError();
      const frames = await this._frames();
      const root = [...frames.values()].find(f => !f.parentId && !f.sessionId);
      const frame = frames.get(record.frameId);
      if (!root || !this.allows(root.url) || !frame || frame.sessionId !== record.sessionId ||
          frame.loaderId !== record.loaderId || frame.url !== record.url || !this.allows(frame.url)) throw new StaleSnapshotError();
      const { nodes = [] } = await this._send(record.sessionId, 'Accessibility.getPartialAXTree', {
        backendNodeId: record.backendNodeId, fetchRelatives: false,
      });
      const node = nodes.find(n => n.backendDOMNodeId === record.backendNodeId && !n.ignored);
      if (!node || node.role?.value !== record.role || node.name?.value?.trim() !== record.name || prop(node, 'disabled') === true) throw new StaleSnapshotError();
      const current = await this._node(record);
      if (current.excluded || current.sensitive || current.disabled || current.readonly !== record.readonly ||
          current.editable !== record.editable || current.tag !== record.tag || current.href !== record.href ||
          current.type !== record.type) throw new StaleSnapshotError();
      // 확인 대기 중 인증 폼이 새로 생겼어도 실행하지 않는다.
      for (const f of frames.values()) {
        if (this.allows(f.url) && (await this._inspect(f)).password) throw new StaleSnapshotError();
      }
      this._assertSnapshot(snapshotId);
      return { ...record, frames, snapshotId };
    }

    async _checkFrameOwners(record, point) {
      let frame = record.frames.get(record.frameId);
      if (!frame) throw new StaleSnapshotError();
      while (frame.parentId) {
        const parent = record.frames.get(frame.parentId);
        if (!parent || !this.allows(parent.url)) throw new StaleSnapshotError();
        const { backendNodeId } = await this._send(parent.sessionId, 'DOM.getFrameOwner', { frameId: frame.id });
        const hit = await this._object({ ...parent, backendNodeId }, `function(p){
          for(let n=this;n;n=n.parentElement||n.getRootNode()?.host){const s=getComputedStyle(n);if(s.transform!=='none'||Number(s.zoom||1)!==1)return {ok:false};}
          const r=this.getBoundingClientRect();const x=r.x+this.clientLeft+p.x,y=r.y+this.clientTop+p.y;
          let h=this.ownerDocument.elementFromPoint(x,y);
          while(h?.shadowRoot){const next=h.shadowRoot.elementFromPoint(x,y);if(!next||next===h)break;h=next;}
          return {ok:h===this,x,y};
        }`, [point]);
        if (!hit?.ok) throw new StaleSnapshotError('프레임이 다른 요소에 가려져 있어요. 다시 확인할게요.');
        point = hit;
        frame = parent;
      }
      return point;
    }

    async click(ref, snapshotId) {
      const r = await this._validate(ref, snapshotId);
      if (this.driver) {
        await this._elementAction('click', r, ref, snapshotId);
        this._invalidate();
        return true;
      }
      await this._send(r.sessionId, 'DOM.scrollIntoViewIfNeeded', { backendNodeId: r.backendNodeId });
      const hit = await this._object(r, `function(){
        for(let n=this;n;n=n.parentElement||n.getRootNode()?.host){const s=getComputedStyle(n);if(s.transform!=='none'||Number(s.zoom||1)!==1)return {ok:false};}
        const r=Array.from(this.getClientRects()).find(r=>r.width>0&&r.height>0);if(!r)return {ok:false};
        const x=r.x+r.width/2,y=r.y+r.height/2;let h=this.ownerDocument.elementFromPoint(x,y);
        while(h?.shadowRoot){const next=h.shadowRoot.elementFromPoint(x,y);if(!next||next===h)break;h=next;}
        return {ok:h===this||this.contains(h),x,y};
      }`);
      if (!hit?.ok) throw new StaleSnapshotError('요소의 클릭 위치를 확인하지 못했어요. 다시 읽을게요.');
      const { x, y } = await this._checkFrameOwners(r, hit);
      this._assertSnapshot(snapshotId);
      await this._send('', 'Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
      await this._send('', 'Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
      this._invalidate();
      return true;
    }

    async fill(ref, text, snapshotId) {
      if (typeof text !== 'string' || text.length > 200) throw new Error('입력 내용이 올바르지 않아요.');
      const r = await this._validate(ref, snapshotId);
      if (!r.editable) throw new Error('이 입력은 직접 처리해주세요.');
      if (this.driver) {
        await this._elementAction('fill', r, ref, snapshotId, text);
        this._invalidate();
        return;
      }
      const ready = await this._object(r, 'function(){this.focus();this.select();return this.getRootNode().activeElement===this;}');
      if (!ready) throw new StaleSnapshotError();
      this._assertSnapshot(snapshotId);
      await this._send(r.sessionId, 'Input.insertText', { text });
      this._invalidate();
    }

    async _elementAction(method, record, ref, snapshotId, text) {
      const attribute = `data-idly-action-${crypto.randomUUID()}`;
      const marked = await this._object(record, 'function(attribute){if(!this.isConnected)return false;this.setAttribute(attribute, "");return true;}', [attribute]);
      if (!marked) throw new StaleSnapshotError();
      try {
        await this.driver.elementAction(method, `[${attribute}]`, url => this.allows(url),
          async phase => {
            await this.beforeAction?.();
            const current = await this._validate(ref, snapshotId);
            if (phase === 'fill-input') await this._assertFocus(current);
            return current;
          }, text, record);
      } finally {
        await this._object(record, 'function(attribute){this.removeAttribute(attribute);}', [attribute]).catch(() => {});
      }
    }

    async _assertFocus(record) {
      if (!await this._object(record, 'function(){return this.isConnected && this.ownerDocument.hasFocus() && this.getRootNode().activeElement===this;}')) throw new StaleSnapshotError('입력 포커스가 바뀌었어요. 다시 확인할게요.');
      let frame = record.frames.get(record.frameId);
      while (frame?.parentId) {
        const parent = record.frames.get(frame.parentId);
        if (!parent) throw new StaleSnapshotError();
        const { backendNodeId } = await this._send(parent.sessionId, 'DOM.getFrameOwner', { frameId: frame.id });
        if (!await this._object({ ...parent, backendNodeId }, 'function(){return this.getRootNode().activeElement===this;}')) throw new StaleSnapshotError('입력 프레임의 포커스가 바뀌었어요.');
        frame = parent;
      }
      this._assertSnapshot(record.snapshotId);
    }

    async _ownedRoot() {
      const frames = await this._frames();
      const root = [...frames.values()].find(f => !f.parentId && !f.sessionId);
      if (!root || !this.allows(root.url)) throw new StaleSnapshotError();
      return root;
    }

    async scroll(direction, snapshotId) {
      if (!['up', 'down'].includes(direction)) throw new Error('스크롤 방향이 올바르지 않아요.');
      if (snapshotId != null) this._assertSnapshot(snapshotId);
      await this._ownedRoot();
      const { cssVisualViewport: viewport } = await this._send('', 'Page.getLayoutMetrics');
      await this._send('', 'Input.synthesizeScrollGesture', {
        x: viewport.clientWidth / 2, y: viewport.clientHeight / 2,
        yDistance: (direction === 'down' ? -1 : 1) * viewport.clientHeight * 0.75,
        gestureSourceType: 'mouse',
      });
      this._invalidate();
    }

    async goto(url, snapshotId) {
      if (!this.allows(url)) throw new Error('허용한 서비스 안에서만 이동할 수 있어요.');
      if (snapshotId != null) this._assertSnapshot(snapshotId);
      await this._ownedRoot();
      this._invalidate();
      await this._send('', 'Page.navigate', { url });
    }

    async close() {
      const attached = this.attached;
      this.attached = false;
      this._invalidate();
      this.chrome.debugger.onEvent.removeListener(this.onEvent);
      this.chrome.debugger.onDetach.removeListener(this.onDetach);
      if (this.driver) await this.driver.close();
      else if (attached) await this.chrome.debugger.detach({ tabId: this.tabId }).catch(() => {});
    }
  }

  const exported = { BrowserSession, StaleSnapshotError };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  else globalThis.IdlyBrowser = exported;
})();
