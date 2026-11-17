// playwright-crx 0.15.0 embeds a Playwright frame handler which ignores OOP
// targets when Target.attachedToTarget arrives before Page.frameAttached.
// Chrome 153 reproduces this when attaching to an already loaded page.
// Keep this narrow, version-checked build patch separate from upstream files.
export function patchCrx(source, version) {
  if (version !== '0.15.0') throw new Error('Review CRX compatibility patch before upgrading');
  const before = `const frame = this._page.frameManager.frame(targetId);
      if (!frame)
        return;`;
  const after = `let frame = this._page.frameManager.frame(targetId);
      if (!frame && event.targetInfo.parentFrameId) {
        this._onFrameAttached(targetId, event.targetInfo.parentFrameId);
        frame = this._page.frameManager.frame(targetId);
      }
      if (!frame)
        return;`;
  if (source.split(before).length !== 2) throw new Error('CRX patch target changed; review upstream source');
  source = source.replace(before, after);
  const oldTree = `if (this._isMainFrame()) {
          this._handleFrameTree(frameTree);
          this._addRendererListeners();
        }`;
  const newTree = `if (this._isMainFrame()) {
          this._handleFrameTree(frameTree);
          this._addRendererListeners();
        } else {
          this._onFrameNavigated(frameTree.frame, true);
          for (const child of frameTree.childFrames || []) this._handleFrameTree(child);
        }`;
  if (source.split(oldTree).length !== 2) throw new Error('CRX frame initialization changed; review upstream');
  source = source.replace(oldTree, newTree);
  const patches = [
    // IDly owns popup adoption/cancellation; avoid unowned autoattached tabs.
    [`if (this._tabToTarget.has(openerTabId))
        await this.attach(id).catch(() => {
        });`, 'return;'],
    // Revalidate after every wait/retry, immediately before physical pointer input.
    ['if (options2.__testHookBeforePointerAction)', 'if (this.__idlyBeforeAction) await this.__idlyBeforeAction();\n      if (options2.__testHookBeforePointerAction)'],
    // Text inputs allowed by IDly use the needsinput path. Also guard each retry.
    ['return await this._retryAction(progress2, "fill", async () => {', 'return await this._retryAction(progress2, "fill", async () => {\n      if (this.__idlyBeforeAction) await this.__idlyBeforeAction();'],
    ['if (result === "needsinput") {', 'if (result === "needsinput") {\n        if (this.__idlyBeforeAction) await this.__idlyBeforeAction("fill-input");'],
  ];
  for (const [before, after] of patches) {
    if (source.split(before).length !== 2) throw new Error('CRX input/popup integration changed; review upstream');
    source = source.replace(before, after);
  }
  return source;
}
