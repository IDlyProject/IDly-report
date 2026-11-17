// Fixed browser operations only; model responses never become JavaScript.
import playwright, { crx } from 'playwright-crx';

let application;
const ACTION_TIMEOUT_MS = 5000;

class CrxDriver {
  constructor(tabId) { this.tabId = tabId; }

  async attach() {
    application ??= crx.start().catch(error => { application = undefined; throw error; });
    this.app = await application;
    this.page = await this.app.attach(this.tabId);
    this.page.setDefaultTimeout(ACTION_TIMEOUT_MS);
  }

  async elementAction(method, selector, allows, validate, text, record) {
    const handles = [];
    let implementation, guardError;
    try {
      for (const frame of this.page.frames()) {
        if (allows(frame.url())) handles.push(...await frame.$$(selector));
      }
      // Keep a fixed ElementHandle: never re-resolve a replacement node after approval.
      if (handles.length !== 1) throw new Error('ambiguous element');
      const element = handles[0];
      // Version-pinned CRX bridge: compare the chosen handle's actual backend
      // identity, not its page-visible selector. Page script can move attributes.
      implementation = playwright._toImpl(element);
      if (!implementation?._objectId || implementation._frame?._id !== record.frameId) throw new Error('frame identity changed');
      const target = record.sessionId ? { tabId: this.tabId, sessionId: record.sessionId } : { tabId: this.tabId };
      const { node } = await chrome.debugger.sendCommand(target, 'DOM.describeNode', { objectId: implementation._objectId });
      if (node.backendNodeId !== record.backendNodeId) throw new Error('node identity changed');
      // The build patch calls this immediately before each physical input,
      // including retries after Playwright has waited for actionability.
      implementation.__idlyBeforeAction = async phase => {
        try { await validate(phase); } catch (error) { guardError = error; throw error; }
      };
      if (method === 'click') {
        await element.click({ trial: true, timeout: ACTION_TIMEOUT_MS });
        await validate();
        await element.click({ timeout: ACTION_TIMEOUT_MS });
      } else if (method === 'fill') {
        await element.waitForElementState('editable', { timeout: ACTION_TIMEOUT_MS });
        await validate();
        await element.fill(text, { timeout: ACTION_TIMEOUT_MS });
      } else throw new Error('unsupported operation');
    } catch (error) {
      if (guardError) throw guardError;
      if (error.name === 'StaleSnapshotError') throw error;
      // Playwright errors can contain page text and call arguments. Do not forward them.
      throw new Error('요소를 안전하게 조작하지 못했어요. 열린 탭에서 직접 이어서 진행해주세요.');
    } finally {
      if (implementation) delete implementation.__idlyBeforeAction;
      await Promise.allSettled(handles.map(handle => handle.dispose()));
    }
  }

  async close() {
    if (this.app) await this.app.detach(this.tabId).catch(() => {});
    this.page = null;
  }
}

globalThis.IdlyCRX = { createDriver: tabId => new CrxDriver(tabId) };
