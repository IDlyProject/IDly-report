"""탐색 결과(analysis.discover_accounts)를 리포트로 바꾼다.

- summarize: 결제 전에 보여주는 무료 요약 숫자
- render_fragment: 리포트 본문 HTML 조각. 결제 전에는 요약 카드와 숫자만, 결제 후에는 전체
  (웹 화면이 그대로 끼워 넣는다. 스타일은 static/report.css)
- render_html / render_pdf: 같은 조각을 문서로 감싸 크로미움으로 A4 PDF로 인쇄한다

메일에서 온 글자(서비스 이름·제목)는 모두 이스케이프한다.
아이콘은 PDF에서도 보이도록 data URI로 넣는다.
"""

import base64
import re
from datetime import date, datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATIC = Path(__file__).parent / "static"
ACTIVE = ("활성", "만료 예정")
FONT_CSS = "https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css"

# 확인할 일 종류별 아이콘·색
KIND_STYLE = {
    "구독": ("ic-event.svg", "t-info"),
    "보안": ("ic-danger.svg", "t-danger"),
    "휴면": ("ic-caution.svg", "t-caution"),
}
KIND_ORDER = {"보안": 0, "구독": 1, "휴면": 2}


@lru_cache(maxsize=None)
def _asset(name: str) -> str:
    data = (STATIC / "assets" / name).read_bytes()
    mime = "image/svg+xml" if name.endswith(".svg") else "image/png"
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _img(name: str) -> str:
    return f'<img src="{_asset(name)}" alt="">'


def _active_subs(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    subs = [a for a in accounts if a.get("subscription") and a["subscription"]["status"] in ACTIVE]
    return sorted(subs, key=lambda a: -(a["subscription"].get("monthly") or 0))


def summarize_many(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """여러 메일함의 분석 결과를 합친다. 같은 구독(서비스·금액이 같은 것)이 두 메일함에 있으면 한 번만 센다."""
    accounts = [a for r in reports for a in r["accounts"]]
    seen = set()
    subs = []
    for a in _active_subs(accounts):
        key = (a["service"].strip().lower(), a["subscription"].get("amount"))
        if key not in seen:
            seen.add(key)
            subs.append(a["subscription"])
    yearly = [x for x in subs if x["cycle"] == "연간"]
    others = [x for x in subs if x["cycle"] != "연간"]
    monthly_charge = sum(x.get("monthly") or 0 for x in others)
    yearly_charge = sum(x.get("amount") or (x.get("monthly") or 0) * 12 for x in yearly)
    return {
        "mailboxes": len(reports),
        "accounts": len(accounts),
        "subscriptions": len(subs),
        "monthly_charge": monthly_charge, "monthly_count": len(others),
        "yearly_charge": yearly_charge, "yearly_count": len(yearly),
        "per_year": monthly_charge * 12 + yearly_charge,
        "security": sum(1 for a in accounts if any(i["kind"] == "보안" for i in a.get("insights", []))),
        "unused": sum(1 for a in accounts if a.get("unused")),
    }


def summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    accounts = report["accounts"]
    subs = _active_subs(accounts)
    yearly = [a["subscription"] for a in subs if a["subscription"]["cycle"] == "연간"]
    others = [a["subscription"] for a in subs if a["subscription"]["cycle"] != "연간"]
    # 월간(주간은 월로 환산)은 매달 나가는 돈, 연간은 1년에 한 번 나가는 돈으로 따로 센다
    monthly_charge = sum(x.get("monthly") or 0 for x in others)
    yearly_charge = sum(x.get("amount") or (x.get("monthly") or 0) * 12 for x in yearly)
    return {
        "messages": report.get("messages_scanned", 0),
        "accounts": len(accounts),
        "subscriptions": len(subs),
        # 연간도 월로 나눠 더한 값 (예전 필드, 정렬·비교용)
        "monthly": sum(a["subscription"].get("monthly") or 0 for a in subs),
        "monthly_charge": monthly_charge,
        "monthly_count": len(others),
        "yearly_charge": yearly_charge,
        "yearly_count": len(yearly),
        "per_year": monthly_charge * 12 + yearly_charge,
        "security": sum(1 for a in accounts if any(i["kind"] == "보안" for i in a.get("insights", []))),
        "unused": sum(1 for a in accounts if a.get("unused")),
        "withdraw": sum(1 for a in accounts if a.get("recommended") == "탈퇴"),
    }


# --- 조각 ----------------------------------------------------------------------

def _e(value: Any) -> str:
    return escape(str(value)) if value not in (None, "") else ""


def _lines(value: Any) -> str:
    """여러 문장이면 문장마다 줄을 바꾼다 (좁은 화면에서 한 덩어리로 길게 흐르지 않게)."""
    return re.sub(r"(?<=[.?!]) +(?=\S)", "<br>", _e(value))


def _won(n: Optional[int]) -> str:
    return f"{n:,}원" if n else "—"


def _d(value: Optional[str]) -> str:
    """2026-10-14 → 2026. 10. 14."""
    if not value:
        return "—"
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return _e(value)
    return f"{d.year}. {d.month}. {d.day}."


def _md(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        d = date.fromisoformat(value[:10])
    except ValueError:
        return ""
    return f"{d.month}/{d.day}"


def _money_line(s: Dict[str, Any]) -> Tuple[str, str]:
    """상단 요약 문구. 연간 결제를 12로 나눠 '매달'에 섞으면 실제로 매달 나가는 돈처럼 읽히므로 따로 말한다."""
    monthly, yearly = s["monthly_charge"], s["yearly_charge"]
    if monthly and yearly:
        return (f"매달 {monthly:,}원 + 1년에 {yearly:,}원",
                f"구독 {s['subscriptions']}개 · 1년이면 모두 약 {s['per_year']:,}원이에요")
    if yearly:
        return (f"1년에 {yearly:,}원",
                f"연간 구독 {s['yearly_count']}개 · 한 달로 치면 약 {round(yearly / 12):,}원이에요")
    if monthly:
        return f"매달 {monthly:,}원", f"구독 {s['subscriptions']}개로 나가고 있어요"
    # 나가는 돈을 모르면 그다음으로 중요한 것: 보안 알림 → 안 쓰는 계정 → 찾은 계정 수
    if s["security"]:
        return f"보안 확인이 필요한 계정 {s['security']:,}개", "새 기기 로그인·보안 알림이 본인 것인지 확인해 보세요"
    if s["unused"]:
        return f"안 쓰는 계정 {s['unused']:,}개", "1년 넘게 활동이 없거나 휴면 안내가 온 계정이에요"
    return f"계정 {s['accounts']:,}개를 찾았어요", "나가는 구독과 보안 위험은 없었어요"


def _hero(report: Dict[str, Any], s: Dict[str, Any], created: datetime) -> str:
    chip = f'<span class="rp-chip">{_img("ic-ai.svg")}AI 분석</span>' if report.get("ai_used") else ""
    sample = '<span class="rp-chip">샘플</span>' if report.get("sample") else ""
    label, desc = _money_line(s)
    # 샘플은 내 메일 분석 결과로 오해하지 않게 리포트 맨 위에 못 박는다
    notice = ('<p class="rp-sample-note"><b>예시 리포트예요.</b> 가상의 사용자 데이터로, '
              '내 메일을 분석한 결과가 아니에요.</p>') if report.get("sample") else ""
    return f"""{notice}
<section class="rp-hero">
  <div class="rp-hero-top"><span class="rp-date">{created.year}년 {created.month}월 {created.day}일 기준</span><span class="rp-chips">{sample}{chip}</span></div>
  <div class="rp-ring"><div><b>{s['accounts']}</b><span>찾은 계정</span></div></div>
  <p class="rp-label">{label}</p>
  <p class="rp-desc">{desc}</p>
</section>"""


def _stats(s: Dict[str, Any]) -> str:
    items = [
        ("ic-event.svg", "t-info", s["subscriptions"], "구독 중"),
        ("ic-danger.svg", "t-danger", s["security"], "보안 알림"),
        ("ic-caution.svg", "t-caution", s["unused"], "안 쓰는 계정"),
    ]
    cells = "".join(
        f'<div class="rp-stat"><span class="rp-bubble {tint}">{_img(icon)}</span><b>{n}</b><span class="rp-stat-label">{label}</span></div>'
        for icon, tint, n, label in items
    )
    return f'<div class="rp-stats">{cells}</div>'


def _subscriptions(accounts: List[Dict[str, Any]]) -> str:
    subs = _active_subs(accounts)
    if not subs:
        return '<h2 class="rp-h2">구독</h2><div class="rp-card"><p class="rp-empty">결제가 이어지는 구독을 찾지 못했어요.</p></div>'
    rows = []
    for a in subs:
        sub = a["subscription"]
        cycle = sub["cycle"] + ("" if sub.get("cycleKnown", True) else "(추정)")
        nxt = f" · 다음 결제 {_d(sub['nextBilling'])}" if sub.get("nextBilling") else ""
        charge = sub.get("amount") or sub.get("monthly")
        per = f"월 {sub['monthly']:,}원" if sub["cycle"] != "월간" and sub.get("monthly") else _e(cycle)
        # 결제 메일은 있는데 금액을 못 읽은 구독: 빈칸 대신 확인할 일로 보여 준다
        text, price = f"{_e(cycle)} 결제{nxt}", (_won(charge) if charge else "확인 필요")
        if sub.get("trial"):
            # 체험 중: 지금은 0원, 금액은 체험이 끝난 뒤 가격
            text = "무료 체험 중" + (f" · 유료 전환 {_d(sub['trialEnd'])}" if sub.get("trialEnd") else "")
            price, per = "0원", (f"이후 {_e(cycle)} {sub['amount']:,}원" if sub.get("amount") else "무료 체험")
        rows.append(
            f'<div class="rp-row"><span class="rp-bubble t-info">{_img("ic-event.svg")}</span>'
            f'<div class="rp-row-body"><p class="rp-title">{_e(a["service"])}</p>'
            f'<p class="rp-text">{text}</p></div>'
            f'<p class="rp-amount">{price}<span>{per}</span></p></div>'
        )
    return f'<h2 class="rp-h2">구독 <small>{len(subs)}</small></h2><div class="rp-card">{"".join(rows)}</div>'


def _todo(accounts: List[Dict[str, Any]]) -> str:
    items = [(a, i) for a in accounts for i in a.get("insights", [])]
    if not items:
        return '<h2 class="rp-h2">확인할 일</h2><div class="rp-card"><p class="rp-empty">지금 확인할 일은 없어요.</p></div>'
    items.sort(key=lambda x: (KIND_ORDER.get(x[1]["kind"], 9), x[1].get("date") or ""))
    rows = []
    for a, i in items:
        icon, tint = KIND_STYLE.get(i["kind"], ("ic-caution.svg", "t-caution"))
        rows.append(
            f'<div class="rp-row"><span class="rp-bubble {tint}">{_img(icon)}</span>'
            f'<div class="rp-row-body"><div class="rp-row-head"><p class="rp-title">{_e(a["service"])} · {_e(i["kind"])}</p>'
            f'<span class="rp-meta">{_md(i.get("date"))}</span></div>'
            f'<p class="rp-text">{_lines(i["status"])}</p><p class="rp-advice">{_e(i["advice"])}</p></div></div>'
        )
    return (f'<h2 class="rp-h2">확인할 일 <small>{len(items)}</small></h2>'
            f'<p class="rp-note">구독·보안·휴면 안내 중 최근 것만 골랐어요.</p><div class="rp-card">{"".join(rows)}</div>')


def _unused(accounts: List[Dict[str, Any]]) -> str:
    unused = sorted((a for a in accounts if a.get("unused")), key=lambda a: a.get("lastLogin") or a.get("lastSeen") or a.get("signupDate") or "")
    if not unused:
        return ""
    rows = []
    for a in unused:
        rec = a.get("recommended")
        dot = "d-danger" if rec == "탈퇴" else "d-caution" if rec else "d-info"
        advice = {"탈퇴": "탈퇴를 고려해 보세요", "구독 해지": "구독 해지를 고려해 보세요"}.get(rec or "", "")
        # 로그인 메일이 있으면 그 날짜, 없으면 마지막으로 받은 메일 날짜 ("로그인 기록 없음"은 로그인한 적이 없다는 뜻으로 읽힌다)
        if a.get("lastLogin"):
            last = f"마지막 로그인 알림 {_d(a['lastLogin'])}"
        elif a.get("lastSeen"):
            last = f"마지막으로 받은 메일 {_d(a['lastSeen'])}"
        else:
            last = "최근 활동 메일 없음"
        rows.append(
            f'<div class="rp-row"><span class="rp-dot {dot}"></span><div class="rp-row-body">'
            f'<p class="rp-title">{_e(a["service"])}</p><p class="rp-text">{last} · 가입 {_d(a.get("signupDate"))}</p>'
            + (f'<p class="rp-advice">{advice}</p>' if advice else "") + "</div></div>"
        )
    return (f'<h2 class="rp-h2">안 쓰는 계정 <small>{len(unused)}</small></h2>'
            '<p class="rp-note">1년 넘게 활동 메일이 없거나 휴면 안내가 온 계정이에요.<br>탈퇴는 휴면 안내가 왔거나 2년 넘게 활동이 없을 때만 권해요.</p>'
            f'<div class="rp-card">{"".join(rows)}</div>')


def _connected_apps(report: Dict[str, Any]) -> str:
    apps = report.get("connected_apps") or []
    if not apps:
        return ""
    rows = "".join(
        f'<div class="rp-row"><span class="rp-dot d-info"></span><div class="rp-row-body"><div class="rp-row-head">'
        f'<p class="rp-title">{_e(app["app"])}</p><span class="rp-meta">{_d(app.get("date"))} 연결</span></div></div></div>'
        for app in apps
    )
    note = ("Google 계정으로 로그인하면서 계정 정보를 공유한 서비스예요. 지금 쓰지 않는 서비스는 "
            "myaccount.google.com/connections에서 연결을 끊어 두세요.")
    return (f'<h2 class="rp-h2">Google로 로그인한 서비스 <small>{len(apps)}</small></h2>'
            f'<p class="rp-note">{_lines(note)}</p><div class="rp-card rp-compact">{rows}</div>')


def _all_accounts(accounts: List[Dict[str, Any]]) -> str:
    ordered = sorted(accounts, key=lambda a: (a.get("category") or "기타", a["service"]))
    rows = "".join(
        f'<div class="rp-row"><div class="rp-row-body"><div class="rp-row-head"><p class="rp-title">{_e(a["service"])}</p>'
        f'<span class="rp-meta">{_e(a.get("category") or "기타")} · 가입 {_d(a.get("signupDate"))}'
        f'{" · Google 로그인" if a.get("permissions") else ""}</span></div></div></div>'
        for a in ordered
    )
    return f'<h2 class="rp-h2">찾은 계정 전체 <small>{len(accounts)}</small></h2><div class="rp-card rp-compact">{rows}</div>'


def _method(report: Dict[str, Any], s: Dict[str, Any]) -> str:
    ai = "규칙 분석과 AI 판별로" if report.get("ai_used") else "규칙 분석으로"
    return (
        '<div class="rp-method">'
        f"<p>메일 {s['messages']:,}통의 보낸 곳·제목·날짜를 {ai} 서비스별로 묶었어요.<br>"
        "결제 메일은 서비스마다 최근 몇 통만 본문을 읽어 금액과 다음 결제일을 확인했어요.</p>"
        "<p>메일만 보고 추정한 결과라 실제와 다를 수 있어요.<br>다른 메일 주소로 가입했거나 결제 메일을 받지 않는 서비스는 빠져 있어요.<br>"
        "달러 결제는 1달러 1,400원으로 환산했어요.<br>IDly는 메일 원문과 메일 비밀번호를 저장하지 않아요.</p>"
        "</div>"
    )


def render_fragment(report: Dict[str, Any], created: datetime, full: bool) -> str:
    accounts = report["accounts"]
    s = summarize(report)
    parts = [_hero(report, s, created), _stats(s)]
    if full:
        parts += [_subscriptions(accounts), _todo(accounts), _connected_apps(report), _unused(accounts),
                  _all_accounts(accounts), _method(report, s)]
    return f'<div class="rp">{"".join(parts)}</div>'


# --- PDF -----------------------------------------------------------------------

PDF_CSS = """
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: #fff; font-family: "Pretendard Variable", Pretendard, "Malgun Gothic", sans-serif; }
.pdf-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.pdf-head p { margin: 0; font-size: 12px; color: #8c8f96; text-align: right; }
.rp-card, .rp-stat { box-shadow: 0 0 0 1px #eceef2; }
"""


def render_html(report: Dict[str, Any], email: str, created: datetime) -> str:
    css = (STATIC / "report.css").read_text(encoding="utf-8")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>IDly 계정 리포트</title>
<link rel="stylesheet" href="{FONT_CSS}">
<style>{css}{PDF_CSS}</style></head>
<body>
<div class="pdf-head">{_img("logo-wordmark.svg")}<p>IDly 계정 리포트<br>{_e(email)}</p></div>
{render_fragment(report, created, full=True)}
</body></html>"""


def render_pdf(html: str) -> bytes:
    """크로미움으로 HTML을 A4 PDF로 인쇄한다. 웹 폰트를 받을 때까지 기다린다."""
    from playwright.sync_api import sync_playwright

    footer = ('<div style="width:100%;font-size:9px;color:#999;padding:0 16mm;'
              'display:flex;justify-content:space-between;font-family:sans-serif">'
              '<span>IDly 계정 리포트</span><span><span class="pageNumber"></span> / <span class="totalPages"></span></span></div>')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            page.evaluate("document.fonts.ready")
            return page.pdf(
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=footer,
                margin={"top": "16mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
            )
        finally:
            browser.close()
