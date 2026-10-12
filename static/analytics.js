// Google Analytics 4. 측정 ID(GA_MEASUREMENT_ID)가 없으면 아무것도 싣지 않고 track()은 조용히 넘어간다.
// 보내지 않는 것: 메일 주소·도메인, 리포트 id, 게스트 토큰, 할인 코드, 선택 정보(나이대·성별·하는 일), 리포트 내용
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

function track(name, params = {}) {
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
