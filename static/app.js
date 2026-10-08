// 화면: intro → connect → scanning → preview(요약·결제) → report. 도움말은 #help.
// 주소: #connect, #r=<리포트 id>. 새로고침하면 테스트를 위해 intro로 돌아간다.
const $ = (sel) => document.querySelector(sel);
const won = (n) => `${n.toLocaleString("ko-KR")}원`;
let config = { price: 4900, payment_mode: "mock", sample: false };
let providers = [];
let pollTimer = null;
// 방금 결제한 리포트: 열자마자 PDF를 내려받는다
let justPaid = null;

// --- 로그인 없이 쓰기 ----------------------------------------------------------------
// 계정 대신 이 브라우저의 localStorage에 무작위 토큰을 두고, 서버는 그 토큰(의 해시)으로 리포트 주인을 가린다.
// 브라우저 데이터를 지우거나 다른 기기에서 열면 토큰이 없어서 리포트를 다시 열 수 없다 → 시작 전에 팝업으로 알린다
const GUEST_KEY = "idly.guest";
let guest = null; // { token, terms } (terms: 약관 동의 시각)
// 저장이 막힌 브라우저(시크릿 창·사이트 데이터 차단): 이 창이 열려 있는 동안만 쓴다
let guestVolatile = false;

function storageWorks() {
  try {
    localStorage.setItem("idly.check", "1");
    localStorage.removeItem("idly.check");
    return true;
  } catch {
    return false;
  }
}

function loadGuest() {
  try {
    const saved = JSON.parse(localStorage.getItem(GUEST_KEY) || "null");
    guest = saved && typeof saved.token === "string" && saved.token.length >= 32 ? saved : null;
  } catch {
    guest = null;
  }
}

function saveGuest() {
  try {
    localStorage.setItem(GUEST_KEY, JSON.stringify(guest));
    guestVolatile = false;
  } catch {
    guestVolatile = true;
  }
}

function clearGuest() {
  guest = null;
  guestVolatile = false;
  try {
    localStorage.removeItem(GUEST_KEY);
  } catch {
    // 저장이 막혀 있었으면 지울 것도 없다
  }
}

function newGuestToken() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

// 로그인했거나 로그인 없이 쓰기로 한 상태
const signedIn = () => !!(me.user || guest);

async function api(path, options = {}) {
  const method = options.method || "GET";
  let res;
  try {
    res = await fetch(path, {
      headers: {
        "Content-Type": "application/json",
        ...(guest ? { "X-IDly-Guest": guest.token } : {}),
      },
      ...options,
    });
  } catch (e) {
    console.error(
      `[IDly] ${method} ${path} → 네트워크 오류 (서버가 꺼져 있거나 연결이 끊김)`,
      e,
    );
    throw new Error("서버에 연결하지 못했어요. 잠시 후 다시 시도해주세요.");
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    logApiError(method, path, res.status, body);
    const err = new Error(body.detail || "잠시 후 다시 시도해주세요.");
    err.status = res.status;
    throw err;
  }
  return body;
}

// 실패 원인을 개발자 도구 콘솔에 남긴다 (서버가 IDLY_DEBUG=1이면 메일 서버 원문 오류·시도 내역·스택까지)
function logApiError(method, path, status, body) {
  console.group(
    `%c[IDly] ${method} ${path} → ${status}`,
    "color:#ee4e4e;font-weight:bold",
  );
  console.error("사용자에게 보인 메시지:", body.detail);
  const debug = body.debug;
  if (debug) {
    if (debug.attempts && debug.attempts.length) console.table(debug.attempts);
    if (debug.password)
      console.info("비밀번호 입력 점검 (값은 보내지 않음):", debug.password);
    if (debug.traceback) console.error(debug.traceback);
    console.dir(debug);
  }
  console.groupEnd();
}

window.addEventListener("unhandledrejection", (e) =>
  console.error("[IDly] 처리되지 않은 오류", e.reason),
);

function show(id) {
  document
    .querySelectorAll(".screen")
    .forEach((el) => (el.hidden = el.id !== id));
  window.scrollTo(0, 0);
}

// --- 팝업 (로그인 시트·알림) ---------------------------------------------------------
// 열 때 포커스를 안으로 옮기고, 닫으면 연 자리로 돌려준다. Tab은 팝업 안에서만 돌고 Esc로 닫는다

const layers = [];

function openLayer(layer, focusTarget) {
  if (!layer.hidden) return;
  layers.push({ layer, returnTo: document.activeElement });
  layer.hidden = false;
  (focusTarget || layer.querySelector("button, a, input")).focus();
}

function closeLayer(layer) {
  const i = layers.findIndex((l) => l.layer === layer);
  layer.hidden = true;
  if (i < 0) return;
  const [{ returnTo }] = layers.splice(i, 1);
  if (returnTo && returnTo.isConnected && returnTo.offsetParent) returnTo.focus();
}

document.addEventListener("keydown", (e) => {
  const top = layers[layers.length - 1];
  if (!top) return;
  if (e.key === "Escape") return closeLayer(top.layer);
  if (e.key !== "Tab") return;
  const items = [...top.layer.querySelectorAll("button, a[href], input")].filter(
    (x) => !x.disabled && x.offsetParent,
  );
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
});

// 주소를 바꾸고 화면을 그린다. 이미 그 주소면 hashchange가 안 일어나서(예: #home에서 약관·선택 정보를 보던 중) 직접 그린다
function go(hash) {
  if (location.hash.replace("#", "") === hash) route();
  else location.hash = hash;
}

function showError(id, message) {
  const el = $(id);
  el.textContent = message || "";
  el.hidden = !message;
}

// --- 메일 연결 ---------------------------------------------------------------------

// 학교·회사 메일은 서버가 메일 서버(MX)를 보고 알아낸다 (도메인별로 한 번만 묻는다)
const detected = {};
// 사용자가 직접 고른 메일 종류 (자동 판단이 틀렸을 때)
const chosen = {};
let detectTimer = null;
// 학교·회사 메일일 때 고를 수 있는 종류
const KIND_CHOICES = [
  ["google_workspace", "Gmail 기반"],
  ["microsoft_365", "Outlook 기반"],
  ["hiworks", "하이웍스"],
  ["custom", "직접 입력"],
];

function isConsumerDomain(domain) {
  return providers.some((p) => p.domains.includes(domain));
}

function domainOf(email) {
  return (email.split("@")[1] || "").toLowerCase();
}

function providerFor(email) {
  const domain = domainOf(email);
  if (chosen[domain]) return providers.find((p) => p.id === chosen[domain]);
  return (
    providers.find((p) => p.domains.includes(domain)) ||
    detected[domain] ||
    (domain.includes(".") ? providers.find((p) => p.id === "custom") : null)
  );
}

function detectLater() {
  clearTimeout(detectTimer);
  const email = $("#connect-form").email.value.trim();
  const domain = domainOf(email);
  if (
    !/\.[a-z]{2,}$/.test(domain) ||
    detected[domain] ||
    providers.some((p) => p.domains.includes(domain))
  )
    return;
  detectTimer = setTimeout(async () => {
    try {
      detected[domain] = await api(
        `/api/providers/detect?email=${encodeURIComponent(email)}`,
      );
      if (domainOf($("#connect-form").email.value.trim()) === domain)
        renderGuide();
    } catch {
      // 알아내지 못하면 직접 입력 칸을 그대로 둔다
    }
  }, 500);
}

function renderKindPicker(domain, provider) {
  const picker = $("#kind-picker");
  // 네이버·Gmail 같은 개인 메일은 고를 필요가 없다
  picker.hidden =
    !provider || !/\.[a-z]{2,}$/.test(domain) || isConsumerDomain(domain);
  if (picker.hidden) return;
  const guessed =
    !chosen[domain] && detected[domain] && detected[domain].guessed;
  $("#kind-note").textContent = guessed
    ? "메일 서버 정보로 추정했어요. 다르면 바꿔 주세요."
    : "학교·회사 메일이 어떤 서비스 위에서 돌아가는지 골라 주세요.";
  const row = $("#kind-options");
  row.innerHTML = "";
  KIND_CHOICES.forEach(([id, label]) => {
    const b = Object.assign(document.createElement("button"), {
      type: "button",
      textContent: label,
      className: "chip",
    });
    b.setAttribute("aria-pressed", String(provider.id === id));
    b.addEventListener("click", () => {
      chosen[domain] = id;
      renderGuide();
    });
    row.append(b);
  });
}

function renderGuide() {
  const form = $("#connect-form");
  const provider = providerFor(form.email.value.trim());
  renderKindPicker(domainOf(form.email.value.trim()), provider);
  const guide = $("#provider-guide");
  const blocked = provider && !provider.auth.includes("password");
  $("#host-field").hidden = !provider || provider.id !== "custom";
  $("#password-label").textContent = provider
    ? provider.password_label
    : "앱 비밀번호";
  $('[form="connect-form"]').disabled = !!blocked;
  guide.classList.toggle("blocked", !!blocked);
  if (!provider || (!blocked && provider.steps.length === 0)) {
    guide.hidden = true;
    return;
  }
  guide.hidden = false;
  if (blocked) {
    guide.textContent = `${provider.name}은 비밀번호로 메일을 읽는 방식을 막아 두어 연동할 수 없어요. 다른 메일 주소로 해주세요.`;
    return;
  }
  guide.innerHTML = "";
  const title = document.createElement("p");
  title.textContent = `${provider.name}은 먼저 이렇게 준비해 주세요`;
  const ol = document.createElement("ol");
  provider.steps.forEach((step) => {
    const li = document.createElement("li");
    if (step.url) {
      li.append(
        Object.assign(document.createElement("a"), {
          href: step.url,
          target: "_blank",
          rel: "noopener",
          textContent: step.text,
        }),
      );
    } else {
      li.textContent = step.text;
    }
    ol.append(li);
  });
  guide.append(title, ol);
}

async function onConnect(event) {
  event.preventDefault();
  const form = event.target;
  const email = form.email.value.trim();
  showError("#connect-error", "");
  if (!email || !form.password.value)
    return showError("#connect-error", "메일 주소와 비밀번호를 입력해주세요.");
  if (!form.consented.checked)
    return showError("#connect-error", "메일 분석 동의에 체크해주세요.");
  const provider = providerFor(email);
  const button = $('[form="connect-form"]');
  button.disabled = true;
  button.textContent = "메일 서버에 연결하는 중…";
  try {
    const { id } = await api("/api/reports", {
      method: "POST",
      body: JSON.stringify({
        email,
        password: form.password.value,
        provider: provider ? provider.id : null,
        host: form.host.value.trim() || null,
        port: Number(form.port.value) || 993,
        consented: true,
        // 로그인 없이 쓸 때는 약관 동의를 계정 대신 요청에 담는다
        terms: !!(guest && guest.terms),
      }),
    });
    const navigation = performance.getEntriesByType("navigation")[0];
    if (navigation && navigation.type === "reload") {
      history.replaceState(null, "", `${location.pathname}${location.search}`);
      renderIntro();
    } else {
      route();
    }
    form.password.type = "password";
    $("#reveal").textContent = "보기";
    location.hash = `r=${id}`;
  } catch (err) {
    showError("#connect-error", err.message);
  } finally {
    button.disabled = false;
    button.textContent = "분석 시작하기";
  }
}

// #sample: 샘플 리포트(결제 완료 상태)를 만들어 보여준다 (로그인 없이도 볼 수 있다)
// 결제 화면에서 열었으면 뒤로 가기·"내 리포트 확인하러 가기"가 그 결제 화면으로 돌아간다
let sampleFromPay = false;
function goBack(event) {
  event.preventDefault();
  sampleFromPay = false;
  history.back();
}

async function openSample() {
  const { id } = await api("/api/reports/sample", { method: "POST" });
  location.replace(`#r=${id}`);
}

// --- 로그인·약관 -------------------------------------------------------------------

let me = { user: null, login: {} };

async function refreshMe() {
  me = await api("/api/me");
  return me;
}

function renderIntro() {
  show("intro");
  // 카카오 팝업이 막혀 화면 이동으로 로그인했다가 실패하면 #login-error로 돌아온다
  if (location.hash === "#login-error")
    openLoginSheet("로그인하지 못했어요. 다시 시도해 주세요.");
}

function openLoginSheet(error) {
  $("#login-mock").hidden = (me.login || {}).mode !== "mock";
  // 이미 로그인 없이 쓰는 중이면 그 선택지는 빼고, 로그인하면 리포트를 옮긴다고 알린다
  $("#open-guest").hidden = !!guest;
  $("#login-desc").textContent = guest
    ? "로그인하면 이 브라우저에 저장한 리포트를 계정으로 옮겨 언제든 다시 볼 수 있어요."
    : "만든 리포트를 계정에 모아 두고 언제든 다시 볼 수 있어요.";
  showError("#login-error", error || "");
  openLayer($("#login-sheet"), $('[data-login="kakao"]'));
}

function closeLoginSheet() {
  closeLayer($("#login-sheet"));
}

// 로그인 없이 사용하기 → 저장 안내 팝업 (저장이 막힌 브라우저면 경고를 같이 보여 준다)
function openGuestNotice(fromHelp) {
  closeLoginSheet();
  $("#guest-blocked").hidden = storageWorks();
  // 도움말에서 다시 볼 때는 안내만: 버튼을 '확인'으로
  $("#guest-ok").textContent = fromHelp ? "확인" : "확인하고 로그인 없이 시작";
  $("#guest-cancel").hidden = !!fromHelp;
  openLayer($("#guest-modal"), $("#guest-ok"));
}

function onGuestConfirmed() {
  closeLayer($("#guest-modal"));
  if (guest) return;
  guest = { token: newGuestToken(), terms: null };
  saveGuest();
  go("home");
}

// 로그인이 끝나면 (카카오 팝업 창 → postMessage, Apple → 응답) 약관 또는 홈으로
async function afterLogin() {
  closeLoginSheet();
  await refreshMe();
  // 로그인 없이 만든 리포트를 계정으로 옮기고, 이 브라우저의 게스트 토큰은 지운다
  if (guest && me.user) {
    try {
      await api("/api/guest/claim", { method: "POST" });
      clearGuest();
    } catch (err) {
      console.error("[IDly] 게스트 리포트를 계정으로 옮기지 못했어요", err);
    }
  }
  location.hash = me.user && me.user.terms ? "home" : "terms";
  route();
}

// 카카오: 팝업 창에서 카카오 로그인 → 서버 콜백이 이 창에 결과를 알리고 닫힌다 (mongle 웹과 같은 인가 코드 방식)
function onKakaoLogin() {
  if (!(me.login || {}).kakao)
    return showError("#login-error", "카카오 로그인이 아직 설정되지 않았어요.");
  const w = 480;
  const h = 720;
  const popup = window.open(
    "/auth/kakao",
    "idly-kakao",
    `width=${w},height=${h},left=${(screen.width - w) / 2},top=${(screen.height - h) / 2}`,
  );
  // 팝업이 막히면 화면 이동으로 로그인한다 (콜백이 홈·약관으로 보내 준다)
  if (!popup) location.href = "/auth/kakao";
}

window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || e.data.type !== "idly-login")
    return;
  if (e.data.ok) afterLogin();
  else showError("#login-error", e.data.message || "로그인하지 못했어요.");
});

// Apple: Sign in with Apple JS 팝업으로 id_token을 받아 서버에서 검증한다 (mongle 웹과 같은 방식)
let appleSdk = null;
function loadAppleSdk() {
  if (!appleSdk) {
    appleSdk = new Promise((resolve, reject) => {
      if (window.AppleID) return resolve();
      const script = document.createElement("script");
      script.src =
        "https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/ko_KR/appleid.auth.js";
      script.onload = () => resolve();
      script.onerror = () => {
        appleSdk = null;
        reject(new Error("Apple 로그인 스크립트를 불러오지 못했어요."));
      };
      document.head.append(script);
    });
  }
  return appleSdk;
}

async function onAppleLogin() {
  if (!(me.login || {}).apple)
    return showError("#login-error", "Apple 로그인이 아직 설정되지 않았어요.");
  showError("#login-error", "");
  try {
    const [cfg] = await Promise.all([
      api("/auth/apple/config"),
      loadAppleSdk(),
    ]);
    window.AppleID.auth.init({
      clientId: cfg.client_id,
      scope: "name email",
      redirectURI: cfg.redirect_uri,
      state: cfg.state,
      nonce: cfg.nonce,
      usePopup: true,
    });
    const res = await window.AppleID.auth.signIn();
    const idToken = res && res.authorization && res.authorization.id_token;
    if (!idToken) throw new Error("Apple 로그인에 실패했어요.");
    await api("/auth/apple/token", {
      method: "POST",
      body: JSON.stringify({ id_token: idToken, user: res.user || null }),
    });
    await afterLogin();
  } catch (err) {
    // 사용자가 팝업을 닫은 경우는 오류로 보지 않는다
    if (
      err &&
      (err.error === "popup_closed_by_user" ||
        err.error === "user_cancelled_authorize")
    )
      return;
    console.error("[IDly] Apple 로그인 실패", err);
    showError(
      "#login-error",
      (err && err.message) || "Apple 로그인에 실패했어요.\n다시 시도해 주세요.",
    );
  }
}

// 목업 로그인 (AUTH_MODE=mock): 누른 서비스 이름의 테스트 계정으로 로그인
async function onMockLogin(provider) {
  showError("#login-error", "");
  try {
    await api("/auth/mock", {
      method: "POST",
      body: JSON.stringify({ provider }),
    });
    await afterLogin();
  } catch (err) {
    showError("#login-error", err.message);
  }
}

function onLogin(provider) {
  if ((me.login || {}).mode === "mock") return onMockLogin(provider);
  if (provider === "kakao") return onKakaoLogin();
  if (provider === "apple") return onAppleLogin();
  showError("#login-error", "아직 준비 중인 로그인이에요.");
}

function termsBoxes() {
  return [
    ...document.querySelectorAll(
      "#terms-form .agree-list input[type=checkbox]",
    ),
  ];
}

async function onTerms(event) {
  event.preventDefault();
  const form = event.target;
  showError("#terms-error", "");
  if (!(form.terms.checked && form.privacy.checked && form.age14.checked)) {
    return showError("#terms-error", "필수 항목에 모두 동의해 주세요.");
  }
  // 로그인 없이: 동의 시각을 이 브라우저에 두고, 리포트를 만들 때마다 서버에 함께 보낸다
  if (!me.user && guest) {
    guest.terms = new Date().toISOString();
    saveGuest();
    return go("home");
  }
  try {
    await api("/api/terms", {
      method: "POST",
      body: JSON.stringify({ terms: true, privacy: true, age14: true }),
    });
    await refreshMe();
    go("home");
  } catch (err) {
    showError("#terms-error", err.message);
  }
}

// --- 선택 정보 (내부 통계) ---------------------------------------------------------

// 처음(약관 뒤)에는 건너뛸 수 있고, 도움말에서 들어오면 고치거나 비워서 지울 수 있다
const aboutPicked = {};

function renderAbout() {
  const first = !me.user.profile_asked;
  const saved = me.user.profile || {};
  $("#about-back").hidden = first;
  $("#about-save").textContent = first ? "저장하고 시작하기" : "저장하기";
  $("#about-skip").textContent = first ? "건너뛰기" : "답한 내용 모두 지우기";
  $("#about-skip").hidden = !first && !Object.values(saved).some(Boolean);
  showError("#about-error", "");
  document.querySelectorAll("#about-form [data-field]").forEach((row) => {
    const field = row.dataset.field;
    aboutPicked[field] = saved[field] || null;
    row.replaceChildren(
      ...(config.profile_options[field] || []).map((value) => {
        const chip = el("button", "chip", value);
        chip.type = "button";
        chip.setAttribute("aria-pressed", String(aboutPicked[field] === value));
        // 다시 누르면 선택을 푼다 (답하지 않기)
        chip.addEventListener("click", () => {
          aboutPicked[field] = aboutPicked[field] === value ? null : value;
          row.querySelectorAll(".chip").forEach((c) =>
            c.setAttribute("aria-pressed", String(aboutPicked[field] === c.textContent)),
          );
        });
        return chip;
      }),
    );
  });
  show("about");
}

async function saveAbout(values) {
  showError("#about-error", "");
  try {
    await api("/api/profile", { method: "POST", body: JSON.stringify(values) });
    const first = !me.user.profile_asked;
    await refreshMe();
    go(first ? "home" : "help");
  } catch (err) {
    showError("#about-error", err.message);
  }
}

// --- 홈 ----------------------------------------------------------------------------

const STATUS_LABEL = {
  실패: "실패",
  중단됨: "멈춤",
  "받는 중": "분석 중",
  "분석 중": "분석 중",
  대기: "분석 중",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function reportRow(r) {
  const row = el("a", "rp-row home-row");
  row.href = `#r=${r.id}`;
  const when = new Date(r.created);
  const done = r.status === "완료";
  const state = !done
    ? STATUS_LABEL[r.status] || r.status
    : r.paid
      ? "열람 가능"
      : "결제 전";
  const body = el("div", "rp-row-body");
  const head = el("div", "rp-row-head");
  head.append(
    el("p", "rp-title", r.mailbox),
    el(
      "span",
      `state ${r.paid ? "state-paid" : done ? "state-unpaid" : "state-busy"}`,
      state,
    ),
  );
  const date = `${when.getFullYear()}. ${when.getMonth() + 1}. ${when.getDate()}.`;
  const detail = r.summary
    ? `계정 ${r.summary.accounts}개 · ${r.summary.money[0]}`
    : "";
  body.append(head, el("p", "rp-text", detail ? `${date} · ${detail}` : date));
  row.append(body);
  return row;
}

async function renderHome() {
  show("home");
  const list = $("#home-reports");
  let reports = [];
  let total = null;
  try {
    ({ reports, total } = await api("/api/my/reports"));
  } catch (err) {
    list.replaceChildren(el("p", "rp-empty", err.message));
    return;
  }
  // 응답을 받은 뒤에 비운다 (홈이 연달아 두 번 그려져도 목록이 겹치지 않게)
  list.replaceChildren();
  // 위쪽 한 줄: 이름, 메일함이 여러 개면 합계라고 알려 준다
  const who = me.user && me.user.name ? `${me.user.name}님의 메일함` : "내 메일함";
  // 로그인 없이 쓰는 중이면 저장 위치를 늘 알려 준다
  $("#guest-note").hidden = !!me.user;
  if (!me.user) {
    $("#guest-note-title").textContent = guestVolatile
      ? "저장이 막혀 있어 이 창에서만 볼 수 있어요"
      : "이 브라우저에만 저장돼요";
    $("#guest-note-text").textContent = guestVolatile
      ? "창을 닫으면 리포트를 다시 열 수 없어요.\n로그인하면 계정에 남길 수 있어요."
      : "브라우저 데이터를 지우거나 다른 기기에서 열면 리포트를 다시 볼 수 없어요.";
  }
  $("#home-hello").textContent = total && total.mailboxes > 1 ? `${who} ${total.mailboxes}개 합계` : who;
  // 결과가 있으면 하나만 크게: 구독 금액 → 보안 알림 → 안 쓰는 계정 → 찾은 계정 수 (서버 _money_line과 같은 순서)
  $("#home-teaser").hidden = !!total;
  $("#home-headline").hidden = !total;
  if (total) {
    const [label, sub] = total.money;
    $("#home-label").textContent = label;
    $("#home-sub").textContent = sub;
  }
  if (!reports.length) list.append(el("p", "rp-empty", "아래 '새 리포트 만들기'로 첫 리포트를 만들어 보세요."));
  reports.forEach((r) => list.append(reportRow(r)));
}

function renderBusiness() {
  const b = config.business || {};
  $("#business").textContent = [
    b.name && `운영 ${b.name}`,
    b.email && `문의 ${b.email}`,
    b.seller && `결제·판매 ${b.seller}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

async function onLogout() {
  await api("/api/logout", { method: "POST" });
  await refreshMe();
  location.hash = "";
  route();
}

// 도움말: 로그인 전에도 볼 수 있다 (계정 메뉴만 숨긴다)
function renderHelp() {
  $("#help-account").hidden = !me.user;
  $("#help-guest").hidden = !!me.user || !guest;
  $("#help-back").href = signedIn() ? "#home" : "#";
  show("help");
}

function onWithdraw() {
  openLayer($("#withdraw-modal"), $("#withdraw-cancel"));
}

async function onWithdrawConfirmed() {
  closeLayer($("#withdraw-modal"));
  await api("/api/me", { method: "DELETE" });
  await refreshMe();
  location.hash = "";
  route();
}

// --- 리포트 상태별 화면 -------------------------------------------------------------

function renderScanning(r) {
  show("scanning");
  const interrupted = r.status === "중단됨";
  const failed = r.status === "실패" || interrupted;
  const n = (v) => (v || 0).toLocaleString("ko-KR");
  const pct = r.total
    ? Math.min(100, Math.round((r.fetched / r.total) * 100))
    : 0;
  $("#scan-title").textContent = interrupted
    ? "분석이 멈췄어요"
    : failed
      ? "분석하지 못했어요"
      : r.status === "분석 중"
        ? "계정을 찾고 있어요"
        : "메일을 읽고 있어요";
  // 분석 중에는 단계 안 진행률(메일 본문 확인 34/120, AI 판별 2/6)로 막대를 채운다
  const stepPct = r.step_total
    ? Math.min(100, Math.round((r.step_done / r.step_total) * 100))
    : null;
  $("#scan-bar").style.transform =
    `scaleX(${r.status === "분석 중" ? (stepPct ?? 100) / 100 : pct / 100})`;
  // 실패하면 진행 막대와 '자동으로 넘어가요' 안내를 숨기고, 오류 이유와 다시 연결하기만 남긴다
  $("#scan-track").hidden = failed;
  $("#scan-wait").hidden = failed;
  $("#scan-detail").textContent = failed
    ? interrupted
      ? "다시 연결하면 처음부터 다시 분석해요"
      : "아래 내용을 확인하고 다시 연결해 주세요"
    : r.status === "분석 중"
      ? `${r.step || "분석"} 중` +
        (r.step_total
          ? ` · ${n(r.step_done)}/${n(r.step_total)}`
          : ` · 메일 ${n(r.total)}통을 다 읽었어요`)
      : r.total
        ? `${n(r.total)}통 중 ${n(r.fetched)}통 읽음 (${pct}%)`
        : "메일 서버에 연결했어요";
  showError("#scan-error", failed ? r.error : "");
  $("#scan-retry").hidden = !failed;
}

async function renderResult(r) {
  const view = await api(`/api/reports/${r.id}/view`);
  if (view.paid) {
    $("#report-body").innerHTML = view.html;
    // 샘플은 로그인 없이도 볼 수 있다. 돌아갈 곳과 아래 안내를 샘플에 맞춘다
    const backToPay = r.sample && sampleFromPay;
    $("#report-title").textContent = r.sample ? "샘플 리포트 (예시)" : "계정 리포트";
    $("#report-back").href = signedIn() ? "#home" : "#";
    $("#report-back").onclick = backToPay ? goBack : null;
    $("#new-report").textContent = r.sample
      ? "내 메일로 리포트 만들기"
      : "다른 메일로 새 리포트 만들기";
    $("#new-report").href = signedIn() ? "#connect" : "#";
    // 결제 화면에서 온 샘플: 내 리포트로 돌아가는 길만 둔다 (PDF·새 리포트 만들기는 뺀다)
    $("#new-report").parentElement.hidden = backToPay;
    $("#pdf").hidden = backToPay;
    $("#sample-back").hidden = !backToPay;
    $("#pdf").href = `/api/reports/${r.id}/pdf`;
    show("report");
    if (justPaid === r.id) {
      justPaid = null;
      $("#pdf").click();
    }
    return;
  }
  $("#preview-body").innerHTML = view.html;
  // 찾은 계정이 하나도 없으면 결제할 내용이 없다: 결제 대신 다른 메일 연결을 권한다
  const empty = !(r.summary && r.summary.accounts);
  $("#pay-empty").hidden = !empty;
  $("#pay-pitch").hidden = empty;
  $("#pay-retry").hidden = !empty;
  $("#pay").hidden = empty;
  $("#pay-note").hidden = empty;
  // 결제 전이면 언제나 샘플을 볼 수 있게 (찾은 계정이 없을 때도)
  $("#sample-cta").hidden = !config.sample;
  $("#pay").disabled = false;
  $("#pay").textContent = `${won(r.price)} 결제하고 전체 리포트 받기`;
  renderCoffee(r);
  const note = $("#pay-note");
  note.innerHTML = "";
  note.append(
    el(
      "span",
      "",
      (config.payment_mode === "mock"
        ? "개발 모드라 실제로 결제되지 않아요.\n"
        : "결제하면 바로 전체 리포트가 열려요.\n디지털 콘텐츠라 열람 후 단순 변심 환불은 제한돼요.\n") +
        "결제는 해외 결제 대행사 Lemon Squeezy에서 진행돼요. ",
    ),
    Object.assign(el("a", "", "환불 정책"), {
      href: "/legal/refund",
      target: "_blank",
      rel: "noopener",
    }),
  );
  $("#dev-confirm").hidden = !(config.debug && config.payment_mode !== "mock");
  show("preview");
}

// 가격을 커피 한 잔 값과 비교하고, 이 메일함에서 나가는 구독료로 '본전'을 보여 준다
function renderCoffee(r) {
  const s = r.summary || {};
  $("#value-title").textContent = `${won(r.price)}, 커피 한 잔 값이에요`;
  let text;
  if (s.monthly_charge) {
    const avg = Math.round(s.monthly_charge / Math.max(1, s.monthly_count));
    text =
      `이 메일함에서 매달 ${won(s.monthly_charge)}이 구독으로 나가요. ` +
      (avg >= r.price
        ? `평균 구독 하나(월 ${won(avg)})만 정리해도 첫 달에 커피값보다 많이 아껴요.`
        : "안 쓰는 구독 몇 개만 정리해도 금방 커피값을 아껴요.");
  } else if (s.yearly_charge) {
    text = `이 메일함에서 1년에 ${won(s.yearly_charge)}이 구독으로 나가요. 갱신 전에 한 번 확인하는 값이에요.`;
  } else {
    text = `안 쓰는 계정 ${s.unused || 0}개, 보안 알림 ${s.security || 0}건을 한 번에 정리하는 값이에요.`;
  }
  $("#value-text").textContent = text;
}

async function load(id) {
  clearTimeout(pollTimer);
  let r;
  try {
    r = await api(`/api/reports/${id}`);
  } catch (err) {
    if (err.status === 404) return show("missing");
    // 서버가 잠깐 꺼졌거나 연결이 끊김: 분석 화면이면 알려 주고 계속 다시 묻는다
    if (!$("#scanning").hidden)
      $("#scan-detail").textContent =
        "서버와 연결이 끊겼어요 · 다시 연결하는 중";
    pollTimer = setTimeout(() => load(id), 3000);
    return;
  }
  if (r.status === "실패" || r.status === "중단됨") {
    console.group("%c[IDly] 메일 분석 실패", "color:#ee4e4e;font-weight:bold");
    console.error("오류:", r.error);
    if (r.debug) {
      console.error("실패 지점:", r.debug.failed_at);
      console.error(r.debug.traceback);
    }
    console.groupEnd();
  }
  if (r.status !== "완료") {
    renderScanning(r);
    if (r.status !== "실패" && r.status !== "중단됨")
      pollTimer = setTimeout(() => load(id), 1500);
    return;
  }
  await renderResult(r);
}

// --- 결제 (Lemon Squeezy) ----------------------------------------------------------

let payPoll = null;

// 결제사 웹훅이 들어와 결제가 확인될 때까지 기다린다 (최대 3분)
function waitForPayment(id) {
  clearInterval(payPoll);
  const button = $("#pay");
  button.disabled = true;
  button.textContent = "결제 확인 중…";
  const started = Date.now();
  payPoll = setInterval(async () => {
    try {
      const r = await api(`/api/reports/${id}`);
      if (r.paid) {
        clearInterval(payPoll);
        payPoll = null;
        justPaid = id;
        await load(id);
      } else if (Date.now() - started > 180000) {
        clearInterval(payPoll);
        payPoll = null;
        button.disabled = false;
        button.textContent = `${won(r.price)} 결제하고 전체 리포트 받기`;
        showError(
          "#pay-error",
          "결제 확인이 늦어지고 있어요.\n결제했다면 잠시 뒤 홈에서 다시 열어 보세요.\n계속 안 열리면 문의해 주세요.",
        );
      }
    } catch {
      // 잠깐의 연결 오류는 다음 확인에서 다시 본다
    }
  }, 2000);
}

function openCheckout(url, id) {
  // Lemon.js가 있으면 화면 위 결제창, 없으면 결제 페이지로 이동 (결제 후 이 리포트로 돌아온다)
  if (window.createLemonSqueezy) {
    window.createLemonSqueezy();
    window.LemonSqueezy.Setup({
      eventHandler: (e) => {
        if (e.event === "Checkout.Success") waitForPayment(id);
      },
    });
    window.LemonSqueezy.Url.Open(url);
  } else {
    location.href = url;
  }
}

async function onPay() {
  const id = currentId();
  const button = $("#pay");
  button.disabled = true;
  showError("#pay-error", "");
  try {
    const res = await api(`/api/reports/${id}/checkout`, { method: "POST" });
    if (res.paid) {
      justPaid = id;
      return load(id);
    }
    openCheckout(res.url, id);
  } catch (err) {
    showError("#pay-error", err.message);
  } finally {
    if (!payPoll) button.disabled = false;
  }
}

async function onDevConfirm() {
  const id = currentId();
  await api(`/api/reports/${id}/dev-confirm`, { method: "POST" });
  justPaid = id;
  await load(id);
}

async function onPdf(event) {
  // 첫 PDF는 만드는 데 몇 초 걸린다. 그동안 버튼으로 알려준다
  event.preventDefault();
  const link = event.currentTarget;
  if (link.dataset.busy) return;
  link.dataset.busy = "1";
  link.textContent = "PDF 만드는 중…";
  try {
    const res = await fetch(link.href);
    if (!res.ok) throw new Error();
    const blob = await res.blob();
    const name =
      (res.headers.get("Content-Disposition") || "").match(
        /filename="(.+)"/,
      )?.[1] || "IDly-report.pdf";
    const url = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement("a"), {
      href: url,
      download: name,
    });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    link.textContent = "PDF 다운로드";
  } catch {
    link.textContent = "다시 시도";
  } finally {
    delete link.dataset.busy;
  }
}

// --- 시작 --------------------------------------------------------------------------

function currentId() {
  return new URLSearchParams(location.hash.slice(1)).get("r");
}

function route() {
  clearTimeout(pollTimer);
  clearInterval(payPoll);
  payPoll = null;
  const id = currentId();
  const hash = location.hash.replace("#", "");
  if (hash === "sample" && config.sample) return openSample();
  if (hash === "help") return renderHelp();
  // 샘플이 아닌 리포트·연결·홈은 로그인(또는 로그인 없이 사용하기)이 필요하다 (샘플 리포트 주소는 그냥 열린다)
  if (!me.user && !guest) return id ? load(id) : renderIntro();
  // 로그인 없이: 약관 동의만 받고, 선택 정보(내부 통계)는 계정이 없어 묻지 않는다
  if (!me.user) {
    if (!guest.terms) return show("terms");
    if (id) return load(id);
    if (hash === "connect") return show("connect");
    return renderHome();
  }
  if (!me.user.terms) return show("terms");
  // 약관 뒤 한 번: 선택 정보 (건너뛰면 다시 묻지 않는다)
  if (!me.user.profile_asked || hash === "about") return renderAbout();
  if (id) return load(id);
  if (hash === "connect") return show("connect");
  return renderHome();
}

async function init() {
  loadGuest();
  [config, providers] = await Promise.all([
    api("/api/config"),
    api("/api/providers"),
    refreshMe(),
  ]);
  $("#contact-link").href =
    `mailto:${config.contact_email}?subject=${encodeURIComponent("[IDly 문의]")}`;
  $("#contact-mail").textContent = config.contact_email;
  $("#help-price").textContent = won(config.price);
  renderBusiness();
  const form = $("#connect-form");
  form.email.addEventListener("input", () => {
    renderGuide();
    detectLater();
  });
  form.addEventListener("submit", onConnect);
  $("#terms-form").addEventListener("submit", onTerms);
  $("#about-form").addEventListener("submit", (e) => {
    e.preventDefault();
    saveAbout(aboutPicked);
  });
  // 처음엔 건너뛰기, 나중엔 모두 지우기: 둘 다 빈 값으로 저장한다
  $("#about-skip").addEventListener("click", () => saveAbout({ age: null, gender: null, job: null }));
  // 전체 동의 ↔ 항목
  $("#terms-all").addEventListener("change", (e) =>
    termsBoxes().forEach((b) => (b.checked = e.target.checked)),
  );
  termsBoxes().forEach((b) =>
    b.addEventListener(
      "change",
      () => ($("#terms-all").checked = termsBoxes().every((x) => x.checked)),
    ),
  );
  $("#open-login").addEventListener("click", () => openLoginSheet());
  $("#open-guest").addEventListener("click", () => openGuestNotice(false));
  $("#guest-ok").addEventListener("click", onGuestConfirmed);
  $("#guest-cancel").addEventListener("click", () => {
    closeLayer($("#guest-modal"));
    openLoginSheet();
  });
  $("#guest-info").addEventListener("click", () => openGuestNotice(true));
  // 홈 안내 카드·도움말의 '로그인하고 계정에 보관하기'
  document
    .querySelectorAll("[data-open-login]")
    .forEach((b) => b.addEventListener("click", () => openLoginSheet()));
  document
    .querySelectorAll("[data-login]")
    .forEach((b) =>
      b.addEventListener("click", () => onLogin(b.dataset.login)),
    );
  // 바깥을 누르면 팝업을 닫는다 (Esc는 openLayer 쪽에서 처리)
  document.querySelectorAll(".sheet-backdrop, .modal-backdrop").forEach((layer) =>
    layer.addEventListener("click", (e) => {
      if (e.target === layer) closeLayer(layer);
    }),
  );
  $("#logout").addEventListener("click", onLogout);
  $("#withdraw").addEventListener("click", onWithdraw);
  $("#withdraw-ok").addEventListener("click", onWithdrawConfirmed);
  $("#withdraw-cancel").addEventListener("click", () => closeLayer($("#withdraw-modal")));
  $("#pay").addEventListener("click", onPay);
  $("#dev-confirm").addEventListener("click", onDevConfirm);
  // 메일 분석 동의: 눌러야 OpenAI 전송 내용이 펼쳐진다
  $("#consent-toggle").addEventListener("click", () => {
    const open = $("#consent-detail").hidden;
    $("#consent-detail").hidden = !open;
    $("#consent-toggle").setAttribute("aria-expanded", String(open));
    $("#consent-toggle").textContent = open ? "접기" : "자세히";
  });
  // 비밀번호 보기·숨기기 (앱 비밀번호를 옮겨 적을 때 틀리기 쉽다)
  $("#reveal").addEventListener("click", () => {
    const input = form.password;
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    $("#reveal").textContent = showing ? "보기" : "숨기기";
    $("#reveal").setAttribute("aria-pressed", String(!showing));
    $("#reveal").setAttribute(
      "aria-label",
      showing ? "비밀번호 보기" : "비밀번호 숨기기",
    );
  });
  $("#pdf").addEventListener("click", onPdf);
  $("#sample-link").addEventListener("click", () => (sampleFromPay = true));
  $("#sample-back").addEventListener("click", goBack);
  window.addEventListener("hashchange", route);
  route();
}

init();
