// Google Analytics 4와 Meta 픽셀. 각 ID(GA_MEASUREMENT_ID, META_PIXEL_ID)가 없으면 아무것도 싣지 않고 track()은 조용히 넘어간다.
// 둘 다 보내지 않는 것: 메일 주소·도메인, 리포트 id, 게스트 토큰, 할인 코드, 선택 정보(나이대·성별·하는 일), 리포트 내용
window.dataLayer = window.dataLayer || [];
function gtag() {
  window.dataLayer.push(arguments);
}

let gaOn = false;
let gaScreen = "";
let gaLastApiError = "";

// 주소에는 리포트 id(#r=…)가 들어 있어 그대로 보내지 않는다. 화면 이름으로 만든 가상 주소를 쓰고, 검색어는 유입 경로(utm_…, gclid)만 남긴다
function gaLocation() {
  const kept = new URLSearchParams();
  new URLSearchParams(location.search).forEach((value, key) => {
    if (key.startsWith("utm_") || key === "gclid") kept.append(key, value);
  });
  const query = kept.toString();
  return `${location.origin}/${gaScreen}${query ? `?${query}` : ""}`;
}

// 팝업이 막혀 화면 이동으로 로그인하면 이전 주소가 /auth/kakao/callback?code=… 이다. 우리 사이트 안에서 온 경우는 주소를 지운다
function gaReferrer() {
  const from = document.referrer;
  return from.startsWith(location.origin) ? "" : from;
}

function initAnalytics(id) {
  if (!id || gaOn) return;
  gaOn = true;
  const s = document.createElement("script");
  s.async = true;
  s.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;
  document.head.append(s);
  gtag("js", new Date());
  gtag("config", id, {
    // 화면 전환은 trackScreen이 직접 보낸다
    send_page_view: false,
    page_location: gaLocation(),
    page_referrer: gaReferrer(),
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
  });
}

// --- Meta 픽셀 (광고 성과 측정) ---------------------------------------------------------
// META_PIXEL_ID가 없으면 싣지 않는다. 보내는 것은 아래 세 가지뿐이고, 어느 이벤트에도 값(파라미터)을 붙이지 않는다.
//   PageView: 랜딩을 연 것 / StartReport: 시작 버튼을 누른 것 / Lead: 메일 연결 정보를 제출한 것
// 픽셀은 GA와 달리 현재 주소(dl)와 이전 주소(rl)를 그대로 보낸다. 그래서
//   - 리포트 화면(#r=<리포트 id>)이나, 주소에 로그인 코드·할인 코드가 남은 화면에서는 아무것도 보내지 않는다
//   - history가 바뀔 때마다 PageView를 자동으로 보내는 기능(disablePushState)과
//     버튼 문구·페이지 정보를 알아서 긁어 가는 자동 설정(autoConfig)을 끈다
//   - 고급 매칭(이메일·전화번호)은 쓰지 않는다. 메일 연결 화면에 이메일 입력칸이 있으므로
//     이벤트 관리자의 '자동 고급 매칭'도 반드시 꺼 둔다 (코드로는 끌 수 없는 설정)
let metaOn = false;
const META_EVENTS = {
  landing_start_click: ["trackCustom", "StartReport"],
  // 메일 연결 결과를 기다리는 몇 초 동안은 주소가 #connect라서 안전하다. 성공 뒤에는 주소가 #r=<id>로 바뀐다
  connect_submit: ["track", "Lead"],
};

function metaSafe() {
  if (/(^|[#&])r=/.test(location.hash)) return false;
  if (/[?&](code|state|discount|token)=/.test(location.search)) return false;
  const from = document.referrer;
  if (from.startsWith(location.origin) && /[?&](code|state)=/.test(from)) return false;
  return true;
}

function metaSend(kind, name) {
  if (!metaOn || !metaSafe()) return;
  try {
    window.fbq(kind, name);
  } catch {
    // 광고 측정 때문에 화면이 멈추면 안 된다
  }
}

function initMetaPixel(id) {
  if (metaOn || !/^\d{6,20}$/.test(String(id || ""))) return;
  metaOn = true;
  // Meta 기본 설치 코드와 같은 대기열. 스크립트가 실리기 전 호출은 쌓아 두었다가 보낸다
  const fbq = function () {
    if (fbq.callMethod) fbq.callMethod.apply(fbq, arguments);
    else fbq.queue.push(arguments);
  };
  if (!window._fbq) window._fbq = fbq;
  fbq.push = fbq;
  fbq.loaded = true;
  fbq.version = "2.0";
  fbq.queue = [];
  fbq.disablePushState = true;
  window.fbq = fbq;
  const s = document.createElement("script");
  s.async = true;
  s.src = "https://connect.facebook.net/en_US/fbevents.js";
  document.head.append(s);
  fbq("set", "autoConfig", false, String(id));
  fbq("init", String(id));
  metaSend("track", "PageView");
}

function track(name, params = {}) {
  if (META_EVENTS[name]) metaSend(...META_EVENTS[name]);
  if (!gaOn) return;
  try {
    gtag("event", name, { page_location: gaLocation(), page_referrer: gaReferrer(), ...params });
  } catch {
    // 통계 때문에 화면이 멈추면 안 된다
  }
}

// 화면이 바뀔 때만 page_view를 보낸다 (분석 중 화면은 1.5초마다 다시 그려진다)
function trackScreen(id) {
  if (id === gaScreen) return;
  gaScreen = id;
  if (!gaOn) return;
  gtag("set", { page_location: gaLocation(), page_referrer: gaReferrer(), page_title: id });
  track("page_view", { page_title: id });
}

// 같은 오류가 이어지면(연결이 끊긴 채 다시 묻는 중) 한 번만 보낸다. 오류 문구에는 메일 주소가 섞일 수 있어 보내지 않는다
function trackApiError(method, path, status) {
  const endpoint = path.split("?")[0].replace(/\/reports\/(?!sample$)[^/]+/, "/reports/:id");
  const key = `${method} ${endpoint} ${status}`;
  if (key === gaLastApiError) return;
  gaLastApiError = key;
  track("api_error", { method, endpoint, status });
}

function trackApiOk() {
  gaLastApiError = "";
}

// data-ga="이벤트 이름"을 단 버튼·링크는 누르면 그 이름으로 보낸다 (data-ga-label은 label로)
document.addEventListener("click", (e) => {
  const target = e.target.closest && e.target.closest("[data-ga]");
  if (!target) return;
  track(target.dataset.ga, target.dataset.gaLabel ? { label: target.dataset.gaLabel } : {});
});
