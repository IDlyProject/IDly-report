// 랜딩 첫 장면 전용 동작. app.js와 따로 둔다.
// 스크롤만 해도 문장 속 칩이 하나씩 체크되고, 뒤의 빛(오라)과 옆의 답 카드가 바뀐다.
// 넓은 화면에서는 칩에 커서를 올리기만 해도 그 칩의 답으로 바로 바뀐다 (클릭할 필요가 없다).
// 스크립트가 없으면 장면은 고정되지 않고 답 카드가 문장 아래에 차례로 펼쳐진다 (.js가 붙을 때만 고정·전환).
(() => {
  const stage = document.querySelector(".ld-stage");
  const scene = document.getElementById("ld-scene");
  if (!stage || !scene) return;
  document.documentElement.classList.add("js");

  const chips = [...stage.querySelectorAll(".ld-chip")];
  const cards = [...stage.querySelectorAll(".ld-card:not(.ld-card-intro)")];
  const intro = stage.querySelector(".ld-card-intro");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const fine = matchMedia("(hover: hover) and (pointer: fine)");
  const n = chips.length;
  let scrolled = -1; // 스크롤이 정한 상태 (-1: 아직 아무것도, n: 전부 체크)
  let hovered = null; // 커서가 올라간 칩

  function paint() {
    const active = hovered ?? scrolled;
    stage.dataset.s = active < 0 ? "" : String(Math.min(active, n));
    chips.forEach((c, i) => {
      c.classList.toggle("done", i <= (hovered ?? scrolled));
      c.classList.toggle("on", i === active);
    });
    // 카드: 켜진 칩의 답. 다 체크했으면 마지막 답을 그대로 둔다
    const card = active < 0 ? -1 : Math.min(active, n - 1);
    cards.forEach((c, i) => c.classList.toggle("on", i === card));
    if (intro) intro.classList.toggle("on", card < 0);
  }

  // 스크롤: 장면을 지나는 비율로 상태를 정한다 (위에서 조금 내려오면 첫 칩부터)
  let ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      ticking = false;
      const rect = scene.getBoundingClientRect();
      const span = scene.offsetHeight - innerHeight;
      if (span <= 0) return;
      const p = Math.min(1, Math.max(0, -rect.top / span));
      const next = Math.min(n, Math.floor(p * (n + 1.2)) - (p < 0.05 ? 1 : 0));
      if (next !== scrolled) {
        const forward = next > scrolled;
        scrolled = next;
        paint();
        if (forward && next >= 0 && next < n && typeof window.track === "function") window.track("pain_seen", { index: next });
      }
    });
  }
  addEventListener("scroll", onScroll, { passive: true });
  addEventListener("resize", onScroll, { passive: true });

  // 커서: 스크롤과 같은 역할. 문장 줄 위에서 커서를 아래로 내리면 그 줄까지 체크되고, 올리면 풀린다. 장면을 떠나면 스크롤 상태로
  const lineEls = [...stage.querySelectorAll(".ld-line")];
  stage.addEventListener("pointermove", (e) => {
    if (!fine.matches) return;
    let k = -1;
    lineEls.forEach((l, i) => { if (e.clientY >= l.getBoundingClientRect().top - 8) k = i; });
    const next = k < 0 ? null : Math.min(k, n);
    if (next !== hovered) { hovered = next; paint(); }
  });
  stage.addEventListener("pointerleave", () => { hovered = null; paint(); });
  chips.forEach((c, i) => {
    c.tabIndex = 0;
    c.addEventListener("focus", () => { hovered = i; paint(); });
    c.addEventListener("blur", () => { hovered = null; paint(); });
  });
  // 빛 고리: 커서를 움직이면 오렌지 쪽이 커서 방향으로 따라 돈다 (떼면 스크롤 상태의 각도로 돌아간다)
  const ring = stage.querySelector(".ld-ring");
  stage.addEventListener("pointermove", (e) => {
    if (!ring || !fine.matches || reduce.matches) return;
    const r = stage.getBoundingClientRect();
    const dx = e.clientX - (r.left + r.width / 2), dy = e.clientY - (r.top + r.height / 2);
    // 커서가 있는 쪽으로 띠가 살짝 기운다 (왼쪽 코랄, 오른쪽 하늘빛 배치는 유지)
    const deg = Math.max(-18, Math.min(18, (dx / (r.width / 2)) * 18 + (dy / (r.height / 2)) * 6));
    stage.classList.add("chase");
    ring.style.setProperty("--rot", `${deg.toFixed(1)}deg`);
  });
  stage.addEventListener("pointerleave", () => {
    stage.classList.remove("chase");
    if (ring) ring.style.removeProperty("--rot");
  });

  // 1-2, 1-3: 화면을 지나는 만큼(--p) 레이더가 돌고, 알림과 대화가 한 칸씩 나타난다
  const progs = [...document.querySelectorAll("[data-progress]")];
  function onProgress() {
    for (const sec of progs) {
      const r = sec.getBoundingClientRect();
      // 섹션 윗변이 화면 아래 85%에 닿을 때 0, 섹션 가운데가 화면 가운데를 지날 때쯤 1
      const p = Math.min(1, Math.max(0, (innerHeight * 0.85 - r.top) / (innerHeight * 0.85 + r.height * 0.15)));
      sec.style.setProperty("--p", p.toFixed(3));
      const steps = Number(sec.dataset.steps) || 1;
      const at = Math.floor(p * (steps + 0.6));
      sec.querySelectorAll("[data-step]").forEach((el) => el.classList.toggle("in", Number(el.dataset.step) <= at));
    }
  }
  if (progs.length) {
    addEventListener("scroll", () => requestAnimationFrame(onProgress), { passive: true });
    addEventListener("resize", onProgress, { passive: true });
    onProgress();
  }

  // 상단바: 첫 장면이 끝나 다음 섹션이 올라오면 위에서 내려온다
  const topbar = document.querySelector(".ld-top");
  const pop = document.getElementById("ld-pop");
  let popDone = false; // 닫았거나 이미 열어 봤으면 다시 띄우지 않는다
  function onTopbar() {
    const left = scene.getBoundingClientRect().bottom < innerHeight * 0.6;
    if (pop && !popDone) pop.hidden = !left;
  }
  addEventListener("scroll", () => requestAnimationFrame(onTopbar), { passive: true });
  onTopbar();

  // 기업 이야기는 한 단계 안쪽: 처음엔 닫혀 있고, 버튼이나 상단바 '사무실 도입'으로 연다 (스크립트가 없으면 늘 열려 있다)
  const b2b = document.getElementById("ld-office-b2b");
  const b2bOpen = document.getElementById("ld-b2b-open");
  function openB2B(scroll) {
    if (!b2b) return;
    const wasHidden = b2b.hidden;
    b2b.hidden = false;
    if (b2bOpen) b2bOpen.setAttribute("aria-expanded", "true");
    if (wasHidden) {
      b2b.classList.add("opening");
      setTimeout(() => b2b.classList.remove("opening"), 600);
    }
    if (scroll) b2b.scrollIntoView({ behavior: reduce.matches ? "auto" : "smooth", block: "start" });
    if (typeof window.track === "function") window.track("b2b_open");
    popDone = true;
    if (pop) pop.hidden = true;
  }
  if (b2b) {
    b2b.hidden = true;
    if (b2bOpen) b2bOpen.addEventListener("click", () => openB2B(true));
    const closePop = () => {
      if (!pop || pop.hidden) return;
      popDone = true;
      pop.classList.add("leaving");
      setTimeout(() => { pop.hidden = true; pop.classList.remove("leaving"); }, 260);
      if (typeof window.track === "function") window.track("b2b_pop_close");
    };
    const popClose = document.getElementById("ld-pop-close");
    if (popClose) popClose.addEventListener("click", closePop);
    addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });
    // 위로 밀어 올리면 닫힌다 (iOS 알림처럼)
    let sy = null;
    pop.addEventListener("pointerdown", (e) => { sy = e.clientY; });
    pop.addEventListener("pointermove", (e) => {
      if (sy === null) return;
      const dy = Math.min(0, e.clientY - sy);
      pop.style.setProperty("--drag", `${dy}px`);
    });
    const endDrag = (e) => {
      if (sy === null) return;
      const dy = e.clientY - sy; sy = null;
      pop.style.removeProperty("--drag");
      if (dy < -40) closePop();
    };
    pop.addEventListener("pointerup", endDrag);
    pop.addEventListener("pointercancel", endDrag);
    // 상단바·푸터의 #ld-office-b2b 링크: 먼저 열고 나서 app.js가 그쪽으로 스크롤한다
    document.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest('a[href="#ld-office-b2b"]');
      if (a) openB2B(false);
    }, true);
  }

  // 흩어진 계정: 처음엔 흩어져 있고, 스크롤해서 내려오는 만큼 가지런해진다 (--mess 1 → 0)
  const fbox = document.querySelector(".ld-folders");
  if (fbox) {
    const folders = [...fbox.querySelectorAll(".ld-folder")];
    folders.forEach((f, i) => {
      const r = (k) => Math.sin(i * 12.9898 + k * 78.233) * 43758.5453 % 1;
      f.style.setProperty("--sx", `${(r(1) * 90).toFixed(0)}px`);
      f.style.setProperty("--sy", `${(r(2) * 60).toFixed(0)}px`);
      f.style.setProperty("--sr", `${(r(3) * 22).toFixed(0)}deg`);
    });
    const tidy = () => {
      if (reduce.matches) { fbox.style.setProperty("--mess", "0"); return; }
      const t = fbox.getBoundingClientRect().top;
      // 폴더 윗변이 화면 아래(100%)에 있을 때 흩어짐 1, 화면 30% 높이까지 올라오면 0
      const m = Math.min(1, Math.max(0, (t - innerHeight * 0.3) / (innerHeight * 0.6)));
      fbox.style.setProperty("--mess", m.toFixed(3));
    };
    addEventListener("scroll", () => requestAnimationFrame(tidy), { passive: true });
    addEventListener("resize", tidy, { passive: true });
    tidy();
  }

  // 랜딩이 다시 보일 때(다른 화면에서 돌아올 때) 상태를 다시 잰다
  new MutationObserver(() => { onScroll(); if (progs.length) onProgress(); onTopbar(); }).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  paint();
  onScroll();
})();
