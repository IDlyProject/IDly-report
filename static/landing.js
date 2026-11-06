// 랜딩(IDly의 메일함) 전용 동작. app.js와 따로 둔다.
// 1) 첫 화면 예시 받은편지함: IDly가 한 줄씩 읽고, 계정 메일이면 표시하고 '찾은 계정'에 쌓는다
// 2) 'IDly가 보낸 메일': 넓은 화면에서는 왼쪽 목록에서 고르면 오른쪽에서 열린다 (좁은 화면·스크립트 없음: 전부 차례로 펼침)
(() => {
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");

  // --- 1. 받은편지함 읽기 ------------------------------------------------------------
  const list = document.getElementById("ld-mails");
  const rows = list ? [...list.children] : [];
  const countEl = document.getElementById("ld-found-n");
  const foundEl = document.getElementById("ld-found-list");
  const pauseBtn = document.getElementById("ld-pause");
  const figure = list && list.closest(".ld-inbox");
  let at = -1;
  let timer = null;
  let userPaused = false;
  let hovering = false;
  let onScreen = true;

  function reset() {
    rows.forEach((r) => r.classList.remove("reading", "found", "read"));
    foundEl.replaceChildren();
    countEl.textContent = "0";
    at = -1;
    list.scrollTop = 0;
  }

  function mark(row) {
    row.classList.add("found");
    const chip = document.createElement("span");
    chip.textContent = row.dataset.found;
    foundEl.append(chip);
    countEl.textContent = String(foundEl.children.length);
  }

  function step() {
    if (at >= 0) {
      rows[at].classList.remove("reading");
      rows[at].classList.add("read");
    }
    at += 1;
    if (at >= rows.length) {
      // 끝까지 읽으면 잠깐 두었다가 처음부터
      clearInterval(timer);
      timer = setTimeout(() => {
        reset();
        timer = null;
        sync();
      }, 2600);
      return;
    }
    const row = rows[at];
    row.classList.add("reading");
    if (row.dataset.found) mark(row);
    // 읽는 줄이 창 가운데쯤 오도록 목록을 올린다
    const target = row.offsetTop - list.clientHeight * 0.4;
    list.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
  }

  function running() {
    return !userPaused && !hovering && onScreen && document.body.classList.contains("is-landing") && !document.hidden;
  }

  function sync() {
    if (!list) return;
    const go = running();
    if (go && timer === null) timer = setInterval(step, 760);
    if (!go && timer !== null && at < rows.length) {
      clearInterval(timer);
      timer = null;
    }
  }

  function showStatic() {
    // 움직임을 줄이는 설정: 다 읽은 상태로 멈춰 둔다
    clearInterval(timer);
    timer = null;
    reset();
    rows.forEach((r) => {
      r.classList.add("read");
      if (r.dataset.found) mark(r);
    });
    pauseBtn.hidden = true;
  }

  if (list) {
    if (reduce.matches) showStatic();
    else {
      pauseBtn.addEventListener("click", () => {
        userPaused = !userPaused;
        pauseBtn.setAttribute("aria-pressed", String(userPaused));
        pauseBtn.textContent = userPaused ? "계속 읽기" : "멈추기";
        sync();
      });
      figure.addEventListener("mouseenter", () => ((hovering = true), sync()));
      figure.addEventListener("mouseleave", () => ((hovering = false), sync()));
      figure.addEventListener("focusin", () => ((hovering = true), sync()));
      figure.addEventListener("focusout", () => ((hovering = false), sync()));
      document.addEventListener("visibilitychange", sync);
      new IntersectionObserver(([e]) => ((onScreen = e.isIntersecting), sync())).observe(figure);
      new MutationObserver(sync).observe(document.body, { attributes: true, attributeFilter: ["class"] });
      sync();
    }
    reduce.addEventListener("change", () => (reduce.matches ? showStatic() : location.reload()));
  }

  // --- 2. IDly가 보낸 메일 -------------------------------------------------------------
  const box = document.querySelector(".ld-box");
  const tablist = box && box.querySelector('[role="tablist"]');
  const tabs = tablist ? [...tablist.querySelectorAll('[role="tab"]')] : [];
  const panels = tabs.map((t) => document.getElementById(t.getAttribute("aria-controls")));
  const wide = matchMedia("(min-width: 60rem)");

  function open(i, focus) {
    tabs.forEach((t, j) => {
      const on = i === j;
      t.setAttribute("aria-selected", String(on));
      t.tabIndex = on ? 0 : -1;
      panels[j].hidden = !on;
    });
    if (focus) tabs[i].focus();
  }

  if (tabs.length) {
    tablist.hidden = false;
    box.classList.add("tabs");
    open(0, false);
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => open(i, false));
      t.addEventListener("keydown", (e) => {
        const n = tabs.length;
        const next = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: n - 1 }[e.key];
        if (next === undefined) return;
        e.preventDefault();
        open((next + n) % n, true);
      });
    });
    // 랜딩 안 링크(#ld-…): 그 메일을 먼저 열고 나서 app.js가 그쪽으로 스크롤한다
    document.addEventListener(
      "click",
      (e) => {
        const a = e.target.closest && e.target.closest('.landing a[href^="#ld-"]');
        if (!a) return;
        const i = panels.findIndex((p) => p.id === a.getAttribute("href").slice(1));
        if (i >= 0 && wide.matches) open(i, false);
      },
      true,
    );
  }
})();
