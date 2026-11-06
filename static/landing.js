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

  // --- 2. 스크롤 이야기 -------------------------------------------------------------
  // 스크립트가 있을 때만 .js를 붙여 숨김·흐림을 켠다 (없으면 전부 보이는 그대로)
  const landing = document.querySelector(".landing");
  if (!landing || !("IntersectionObserver" in window)) return;
  document.documentElement.classList.add("js");

  // 한 번 나타나기 + 숫자 세기
  const fmt = (n) => n.toLocaleString("ko-KR");
  function countUp(el) {
    const end = Number(el.dataset.count);
    if (reduce.matches || !end) return;
    const start = performance.now();
    const dur = 1100;
    const tick = (now) => {
      const p = Math.min(1, (now - start) / dur);
      el.textContent = fmt(Math.round(end * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick);
    };
    el.textContent = "0";
    requestAnimationFrame(tick);
  }
  const revealIO = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        e.target.classList.add("in");
        e.target.querySelectorAll("[data-count]").forEach(countUp);
        if (e.target.matches("[data-count]")) countUp(e.target);
        revealIO.unobserve(e.target);
      }
    },
    { threshold: 0, rootMargin: "0px 0px -12% 0px" },
  );
  landing.querySelectorAll("[data-reveal]").forEach((el) => revealIO.observe(el));

  // 화면 가운데 띠에 들어온 것 하나만 '켜기' (문제 목록, 솔루션 단계)
  function centerOn(items, onChange) {
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          items.forEach((x) => x.classList.toggle("on", x === e.target));
          if (onChange) onChange(e.target);
        }
      },
      { rootMargin: "-45% 0px -45% 0px" },
    );
    items.forEach((x) => io.observe(x));
  }
  const roll = [...landing.querySelectorAll("[data-roll] li")];
  if (roll.length) centerOn(roll);

  const stages = [...landing.querySelectorAll("[data-stage]")];
  const shots = [...landing.querySelectorAll("[data-stage-img]")];
  if (stages.length) {
    stages[0].classList.add("on");
    centerOn(stages, (li) => {
      shots.forEach((s) => s.classList.toggle("on", s.dataset.stageImg === li.dataset.stage));
    });
  }
})();
