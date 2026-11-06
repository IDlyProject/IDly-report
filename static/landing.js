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
      c.classList.toggle("done", i <= Math.max(scrolled, hovered ?? -1));
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
      const next = Math.min(n, Math.floor(p * (n + 1.2)) - (p < 0.04 ? 1 : 0));
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

  // 커서: 칩에 올리면 그 답으로, 장면 어디서든 빛이 커서 쪽으로 살짝 기운다
  chips.forEach((c, i) => {
    c.addEventListener("mouseenter", () => {
      if (!fine.matches) return;
      hovered = i;
      paint();
    });
    c.addEventListener("focus", () => {
      hovered = i;
      paint();
    });
    c.addEventListener("blur", () => {
      hovered = null;
      paint();
    });
  });
  stage.querySelector(".ld-sentence").addEventListener("mouseleave", () => {
    hovered = null;
    paint();
  });
  stage.addEventListener("pointermove", (e) => {
    if (!fine.matches || reduce.matches) return;
    const r = stage.getBoundingClientRect();
    stage.style.setProperty("--mx", ((e.clientX - r.left) / r.width - 0.5).toFixed(3));
    stage.style.setProperty("--my", ((e.clientY - r.top) / r.height - 0.5).toFixed(3));
  });
  stage.addEventListener("pointerleave", () => {
    stage.style.setProperty("--mx", "0");
    stage.style.setProperty("--my", "0");
  });

  // 랜딩이 다시 보일 때(다른 화면에서 돌아올 때) 상태를 다시 잰다
  new MutationObserver(onScroll).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  paint();
  onScroll();
})();
