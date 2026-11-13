// 랜딩 전용 동작. app.js와 따로 둔다.
// 불편 문장의 칩이 스크롤만으로 하나씩 체크되고, 그 장면을 지나면 "이런 문제는 기업에도 있어요" 알림이 위에서 내려온다.
// 알림이나 상단의 '기업 서비스'를 누르면 기업 소개로 내려간다. 스크립트가 없으면 칩은 체크된 채로 보인다.
(() => {
  const scene = document.getElementById("ld-pains");
  if (!scene) return;
  document.documentElement.classList.add("js");

  const chips = [...scene.querySelectorAll(".ld-chip")];
  const list = scene.querySelector(".ld-lines");
  const lines = [...list.querySelectorAll(".ld-line")];
  const answers = [...scene.querySelectorAll(".ld-answer")];
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const fine = matchMedia("(hover: hover) and (pointer: fine)");
  const n = chips.length;
  let scrolled = -1;
  let hovered = null;

  function paint() {
    const active = hovered ?? scrolled;
    scene.dataset.s = active < 0 ? "" : String(Math.min(active, n));
    chips.forEach((c, i) => {
      c.classList.toggle("done", i <= active);
      c.classList.toggle("on", i === active);
    });
    // 체크된 불편의 답을 켠다. 다 체크하면 답도 전부 켠다
    answers.forEach((a) => a.classList.toggle("on", active >= n || a.dataset.pains.split(" ").includes(String(active))));
  }

  function onScroll() {
    const rect = scene.getBoundingClientRect();
    if (!rect.height) return;
    // 화면 절반에 닿을 때 시작해, 스크롤을 어느 정도 내릴 때까지 나눠서 체크한다 (첫 화면에서 이미 다 체크되지 않게)
    const p = Math.min(1, Math.max(0, (innerHeight * 0.5 - rect.top) / Math.max(innerHeight * 0.42, rect.height - innerHeight * 0.2)));
    const next = p <= 0 ? -1 : Math.min(n, Math.floor(p * (n + 1)));
    if (next !== scrolled) {
      const forward = next > scrolled;
      scrolled = next;
      paint();
      if (forward && next >= 0 && next < n && typeof window.track === "function") window.track("pain_seen", { index: next });
    }
  }

  // 커서: 스크롤과 같은 역할. 문장 줄을 따라 내려가면 그 줄까지 체크된다
  list.addEventListener("pointermove", (e) => {
    if (!fine.matches) return;
    let k = -1;
    lines.forEach((l, i) => { if (e.clientY >= l.getBoundingClientRect().top - 8) k = i; });
    const next = k < 0 ? null : Math.min(k, n);
    if (next !== hovered) { hovered = next; paint(); }
  });
  list.addEventListener("pointerleave", () => { hovered = null; paint(); });

  const pop = document.getElementById("ld-pop");
  const b2b = document.getElementById("ld-office-b2b");
  const b2bOpen = document.getElementById("ld-b2b-open");
  let popDone = false; // 닫았거나 이미 열어 봤으면 다시 띄우지 않는다

  // 휴대폰은 화면이 좁아 알림이 본문을 오래 가리므로, 알림이 가리키는 기업 구역에 닿으면 거둔다
  const phone = matchMedia("(max-device-width: 40rem)");
  function onPop() {
    const left = scene.getBoundingClientRect().bottom < innerHeight * 0.6;
    const reached = phone.matches && b2b && b2b.getBoundingClientRect().top < innerHeight;
    if (pop && !popDone) pop.hidden = !left || reached;
  }

  function openB2B(scroll) {
    if (!b2b) return;
    b2b.hidden = false;
    if (b2bOpen) b2bOpen.setAttribute("aria-expanded", "true");
    if (scroll) b2b.scrollIntoView({ behavior: reduce.matches ? "auto" : "smooth", block: "start" });
    if (typeof window.track === "function") window.track("b2b_open");
    popDone = true;
    if (pop) pop.hidden = true;
  }

  function closePop() {
    if (!pop || pop.hidden) return;
    popDone = true;
    pop.classList.add("leaving");
    setTimeout(() => { pop.hidden = true; pop.classList.remove("leaving"); }, 260);
    if (typeof window.track === "function") window.track("b2b_pop_close");
  }

  if (b2b) {
    if (b2bOpen) b2bOpen.addEventListener("click", () => openB2B(true));
    // 상단바의 #ld-office-b2b 링크: 먼저 열고 나서 app.js가 그쪽으로 스크롤한다
    document.addEventListener("click", (e) => {
      const a = e.target.closest && e.target.closest('a[href="#ld-office-b2b"]');
      if (a) openB2B(false);
    }, true);
  }

  if (pop) {
    const popClose = document.getElementById("ld-pop-close");
    if (popClose) popClose.addEventListener("click", closePop);
    addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });
    // 위로 밀어 올리면 닫힌다 (iOS 알림처럼)
    let startY = null;
    pop.addEventListener("pointerdown", (e) => { startY = e.clientY; });
    pop.addEventListener("pointermove", (e) => {
      if (startY === null) return;
      pop.style.setProperty("--drag", `${Math.min(0, e.clientY - startY)}px`);
    });
    const endDrag = (e) => {
      if (startY === null) return;
      const dy = e.clientY - startY;
      startY = null;
      pop.style.removeProperty("--drag");
      if (dy < -40) closePop();
    };
    pop.addEventListener("pointerup", endDrag);
    pop.addEventListener("pointercancel", endDrag);
  }

  // 크레딧처럼 화면에 들어오는 줄부터 하나씩 올라온다 (한 번 올라오면 그대로 둔다)
  const rolls = document.querySelectorAll(".ld-pains .ld-say");
  if ("IntersectionObserver" in window && !reduce.matches) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (!e.isIntersecting) return;
        e.target.classList.add("in");
        io.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    rolls.forEach((el) => { el.classList.add("ld-roll"); io.observe(el); });
  }

  const onMove = () => { onScroll(); onPop(); };
  addEventListener("scroll", onMove, { passive: true });
  addEventListener("resize", onMove, { passive: true });
  // 다른 화면에서 랜딩으로 돌아오면 상태를 다시 잰다
  new MutationObserver(onMove).observe(document.body, { attributes: true, attributeFilter: ["class"] });
  paint();
  onMove();
})();
