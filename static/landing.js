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

  // --- 2. 이런 거 불편하셨죠? 체크하면 옆 창에 IDly의 답 -------------------------------
  const landing = document.querySelector(".landing");
  const pains = landing ? [...landing.querySelectorAll("[data-pain]")] : [];
  if (!pains.length) return;
  // 스크립트가 있을 때만 답을 항목 아래에서 숨기고 옆 창으로 옮긴다 (없으면 항목 아래에 펼쳐진 그대로)
  document.documentElement.classList.add("js");
  const idle = document.getElementById("ld-idle");
  const ansBox = document.getElementById("ld-panel-ans");
  const verdict = document.getElementById("ld-verdict");
  const verdictBase = verdict.textContent;
  let shown = null;

  function showAnswer(input) {
    shown = input;
    if (!input) {
      ansBox.hidden = true;
      ansBox.replaceChildren();
      idle.hidden = false;
      return;
    }
    const ans = document.getElementById(`ans-${input.dataset.pain}`);
    ansBox.replaceChildren(...[...ans.children].map((c) => c.cloneNode(true)));
    idle.hidden = true;
    ansBox.hidden = false;
  }

  function syncPains(changed) {
    const on = pains.filter((p) => p.checked);
    pains.forEach((p) => p.closest("li").classList.toggle("on", p.checked));
    const n = on.length;
    verdict.textContent =
      n === 0
        ? verdictBase
        : n === pains.length
          ? "전부 해당되시네요. 지금 메일함에서 바로 확인해 보세요."
          : `${n}개 해당되시네요. 지금 메일함에서 바로 확인해 보세요.`;
    // 방금 체크한 것을 보여 주고, 체크를 풀면 남은 것 중 마지막, 없으면 다시 읽는 중 화면
    if (changed && changed.checked) showAnswer(changed);
    else if (!changed || shown === changed) showAnswer(on[on.length - 1] || null);
  }

  pains.forEach((p) => {
    p.addEventListener("change", () => {
      syncPains(p);
      if (p.checked && typeof window.track === "function") window.track("pain_check", { pain: p.dataset.pain, count: pains.filter((x) => x.checked).length });
    });
    // 넓은 화면: 마우스를 올리면 미리 보기 (체크한 게 없을 때만)
    p.closest("li").addEventListener("mouseenter", () => {
      if (!pains.some((x) => x.checked) && matchMedia("(min-width: 60rem)").matches) showAnswer(p);
    });
  });
  document.getElementById("ld-pains").addEventListener("mouseleave", () => {
    if (!pains.some((x) => x.checked)) showAnswer(null);
  });
  syncPains(null);
})();
