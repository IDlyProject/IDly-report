"""리포트 조치 링크: 계정마다 해지·탈퇴·보안 설정 페이지로 바로 가는 링크를 고른다.

고르는 순서 (docs/superpowers/specs/2026-10-19-action-links-design.md):
1. 큐레이션 — 공식 도움말·공식 페이지에서 직접 확인한 링크 (CURATED, 칸마다 확인한 출처를 함께 둔다)
2. 검색 캐시 — 예전에 웹 검색으로 찾아 도메인 검증을 통과한 링크 (DB action_links, 90일)
3. 웹 검색 — 결제한 리포트에서만. 서비스 이름·도메인·조치만 보낸다
4. 구글 검색 대체 링크 — 실패하지 않으므로 버튼이 항상 붙는다

IDly가 사용자를 엉뚱한 사이트로 보내는 것이 가장 나쁜 결과라, 큐레이션 밖의 링크는 is_valid를 통과해야만 쓴다.
서버는 찾은 URL을 직접 가져오지 않는다.
"""

import ipaddress
import os
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urlsplit

import httpx

import db

ACTIONS = ("해지", "탈퇴", "보안")
CACHE_DAYS = 90
ACTIVE = ("활성", "만료 예정")

# 웹 검색 상한: 리포트 하나당
MAX_SEARCHES = 20
SEARCH_WORKERS = 5
SEARCH_TIMEOUT = 10
SEARCH_DEADLINE = 20
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

# App Store 구독(계정 id가 apps.apple.com/…)은 앱마다 해지 페이지가 따로 없고 Apple 구독 관리에서 끈다
APPLE_SUBSCRIPTIONS = "https://apps.apple.com/account/subscriptions"

# 도메인 → {조치: (URL, 확인한 출처)}. 확인하지 못한 칸은 비워 둔다 (추측으로 채우지 않는다)
CURATED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "aladin.co.kr": {
        "탈퇴": ("https://www.aladin.co.kr/account/waccount_guide.aspx",
               "https://www.aladin.co.kr/cs_center/wcs_guide_detail.aspx?pn=account_01"),
    },
    "anthropic.com": {
        "해지": ("https://claude.ai/settings/billing",
               "https://support.claude.com/en/articles/8325617-cancel-your-pro-or-max-subscription"),
        "탈퇴": ("https://claude.ai/settings/account",
               "https://support.claude.com/en/articles/9028421-delete-your-claude-account"),
    },
    "any.do": {
        "탈퇴": ("https://support.any.do/en/articles/9608389-account-security-and-privacy-in-any-do",
               "https://support.any.do/en/articles/9608389-account-security-and-privacy-in-any-do"),
    },
    "apple.com": {
        "해지": ("https://account.apple.com/account/manage/section/subscriptions",
               "https://support.apple.com/en-us/118428"),
        "탈퇴": ("https://privacy.apple.com",
               "https://support.apple.com/en-us/102283"),
        "보안": ("https://account.apple.com",
               "https://support.apple.com/en-us/101567"),
    },
    "aws.amazon.com": {
        "탈퇴": ("https://console.aws.amazon.com/billing/home#/account",
               "https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-closing.html"),
        "보안": ("https://console.aws.amazon.com/iam/home?#security_credential",
               "https://docs.aws.amazon.com/IAM/latest/UserGuide/root-user-password.html"),
    },
    "bandlab.com": {
        "해지": ("https://www.bandlab.com/settings/membership",
               "https://help.bandlab.com/hc/en-us/articles/61321024101017-Manage-your-Subscription"),
        "탈퇴": ("https://www.bandlab.com/settings/account",
               "https://help.bandlab.com/hc/en-us/articles/115003830774-How-do-I-delete-my-account"),
        "보안": ("https://www.bandlab.com/settings/security",
               "https://help.bandlab.com/hc/en-us/articles/10011626053145-How-do-I-improve-the-security-of-my-account"),
    },
    "beatstars.com": {
        "탈퇴": ("https://help.beatstars.com/hc/en-us/articles/360007986854-How-do-I-Close-my-Account",
               "https://help.beatstars.com/hc/en-us/articles/360007986854-How-do-I-Close-my-Account"),
    },
    "behance.net": {
        "탈퇴": ("https://help.behance.net/hc/en-us/articles/204484854-Guide-Delete-Your-Behance-Profile",
               "https://help.behance.net/hc/en-us/articles/204484854-Guide-Delete-Your-Behance-Profile"),
    },
    "bizhows.com": {
        "탈퇴": ("https://help.bizhows.com/hc/ko/articles/56581042688153",
               "https://help.bizhows.com/hc/ko/articles/56581042688153"),
    },
    "clipstudio.net": {
        "탈퇴": ("https://support.clip-studio.com/en-us/faq/articles/20190316",
               "https://support.clip-studio.com/en-us/faq/articles/20190316"),
    },
    "coupang.com": {
        "해지": ("https://loyalty.coupang.com/loyalty/management/home",
               "https://news.coupang.com/archives/64216/"),
        "탈퇴": ("https://login.coupang.com/login/secession.pang",
               "https://news.coupang.com/archives/64377/"),
    },
    "coursera.org": {
        "탈퇴": ("https://www.coursera.support/s/article/209818563-Delete-your-Coursera-account",
               "https://www.coursera.support/s/article/209818563-Delete-your-Coursera-account"),
    },
    "dacon.io": {
        "탈퇴": ("https://dacon.io/more/faq",
               "https://dacon.io/more/faq"),
    },
    "daum.net": {
        "탈퇴": ("https://accounts.kakao.com",
               "https://cs.kakao.com/helps_html/1073185457?locale=ko"),
    },
    "distrokid.com": {
        "해지": ("https://distrokid.com/leave/",
               "https://support.distrokid.com/hc/en-us/articles/360013535374"),
    },
    "dropbox.com": {
        "해지": ("https://www.dropbox.com/account/billing",
               "https://help.dropbox.com/plans/downgrade-dropbox-plus-professional-plans"),
        "탈퇴": ("https://help.dropbox.com/account-access/delete-account",
               "https://help.dropbox.com/account-access/delete-account"),
    },
    "ea.com": {
        "탈퇴": ("https://myaccount.ea.com/am/ui/security-privacy",
               "https://help.ea.com/en/articles/ea-account/delete-ea-account/"),
        "보안": ("https://myaccount.ea.com/am/ui/security-privacy",
               "https://help.ea.com/en/articles/ea-account/delete-ea-account/"),
    },
    "ebs.co.kr": {
        "탈퇴": ("https://sso.ebs.co.kr/secession/auth",
               "https://sso.ebs.co.kr/secession/auth"),
    },
    "etoos.com": {
        "탈퇴": ("https://www.etoos.com/customer/faq_r.asp?cosl_lclass=CS03&idx=14829276",
               "https://www.etoos.com/customer/faq_r.asp?cosl_lclass=CS03&idx=14829276"),
    },
    "evernote.com": {
        "해지": ("https://accounts.evernote.com/settings/account/manage-plans",
               "https://help.evernote.com/hc/en-us/articles/115005949208-How-to-cancel-your-Evernote-subscription"),
        "탈퇴": ("https://accounts.evernote.com/settings/security/summary",
               "https://help.evernote.com/hc/en-us/articles/360056549574-Permanently-close-your-Evernote-account"),
        "보안": ("https://accounts.evernote.com/settings/security/summary",
               "https://help.evernote.com/hc/en-us/articles/360056549574-Permanently-close-your-Evernote-account"),
    },
    "expo.dev": {
        "해지": ("https://expo.dev/settings/billing",
               "https://docs.expo.dev/billing/manage/"),
        "탈퇴": ("https://expo.dev/settings",
               "https://docs.expo.dev/accounts/sso/"),
        "보안": ("https://expo.dev/settings#two-factor-auth",
               "https://docs.expo.dev/accounts/two-factor/"),
    },
    "facebook.com": {
        "탈퇴": ("https://www.facebook.com/help/224562897555674",
               "https://www.facebook.com/help/224562897555674"),
        "보안": ("https://www.facebook.com/settings?tab=security",
               "https://www.facebook.com/help/285695718429403"),
    },
    "google.com": {
        "탈퇴": ("https://myaccount.google.com/deleteaccount",
               "https://support.google.com/accounts/answer/32046"),
        "보안": ("https://myaccount.google.com/security",
               "https://support.google.com/accounts/answer/46526"),
    },
    "grammarly.com": {
        "해지": ("https://account.grammarly.com/subscription",
               "https://support.grammarly.com/hc/en-us/articles/115000090172-How-do-I-cancel-my-subscription"),
        "탈퇴": ("https://account.grammarly.com/profile",
               "https://support.grammarly.com/hc/en-us/articles/115000090052-Delete-your-Grammarly-account"),
    },
    "hackthebox.com": {
        "탈퇴": ("https://help.hackthebox.com/en/articles/9189654-manage-your-htb-account",
               "https://help.hackthebox.com/en/articles/9189654-manage-your-htb-account"),
        "보안": ("https://account.hackthebox.com/security-settings",
               "https://help.hackthebox.com/en/articles/9189654-manage-your-htb-account"),
    },
    "hancom.com": {
        "탈퇴": ("https://support.hancom.com/fcaff86f-6e76-437f-af69-e7c3ec958e10",
               "https://support.hancom.com/fcaff86f-6e76-437f-af69-e7c3ec958e10"),
    },
    "huggingface.co": {
        "보안": ("https://hf.co/settings/authentication",
               "https://huggingface.co/docs/hub/security-2fa"),
    },
    "instagram.com": {
        "탈퇴": ("https://accountscenter.instagram.com/personal_info/account_ownership_and_control",
               "https://help.instagram.com/139886812848894"),
    },
    "kakao.com": {
        "탈퇴": ("https://accounts.kakao.com",
               "https://cs.kakao.com/helps_html/1073185457?locale=ko"),
    },
    "laftel.net": {
        "해지": ("https://help.laftel.net/hc/ko/articles/6011133529231",
               "https://help.laftel.net/hc/ko/articles/6011133529231"),
        "탈퇴": ("https://help.laftel.net/hc/ko/articles/6011122211471",
               "https://help.laftel.net/hc/ko/articles/6011122211471"),
    },
    "lastpass.com": {
        "탈퇴": ("https://lastpass.com/delete_account.php",
               "https://support.lastpass.com/help/delete-your-lastpass-account-lp010012"),
    },
    "linkedin.com": {
        "해지": ("https://linkedin.com/premium/cancel",
               "https://www.linkedin.com/help/learning/answer/63"),
        "탈퇴": ("https://www.linkedin.com/psettings/account-management/close-action-needed",
               "https://www.linkedin.com/help/learning/answer/63"),
        "보안": ("https://www.linkedin.com/mypreferences/d/change-password",
               "https://www.linkedin.com/help/linkedin/answer/a1379143"),
    },
    "manus.im": {
        "해지": ("https://help.manus.im/en/articles/11711087-how-can-i-cancel-my-subscription",
               "https://help.manus.im/en/articles/11711087-how-can-i-cancel-my-subscription"),
        "탈퇴": ("https://help.manus.im/en/articles/11711969-how-can-i-delete-my-account",
               "https://help.manus.im/en/articles/11711969-how-can-i-delete-my-account"),
    },
    "mediafire.com": {
        "탈퇴": ("https://www.mediafire.com/myaccount",
               "https://www.mediafire.com/common_questions/"),
    },
    "megastudy.net": {
        "탈퇴": ("https://www.megastudy.net/help/faq/faq_search_view.asp?varFaqNum=ANSW20030708000001",
               "https://www.megastudy.net/help/faq/faq_search_view.asp?varFaqNum=ANSW20030708000001"),
    },
    "microsoft.com": {
        "해지": ("https://account.microsoft.com/services",
               "https://support.microsoft.com/en-us/accounts-billing/subscriptions/cancel-your-microsoft-subscription"),
        "탈퇴": ("https://go.microsoft.com/fwlink/?linkid=2272280",
               "https://support.microsoft.com/en-us/accounts-billing/manage/how-to-close-your-microsoft-account"),
        "보안": ("https://account.microsoft.com/security",
               "https://support.microsoft.com/en-us/accounts-billing/manage/change-your-microsoft-account-password"),
    },
    "miricanvas.com": {
        "해지": ("https://www.miricanvas.com/ko/workspace/setting/billing",
               "https://help.miricanvas.com/hc/ko/articles/6121039777817"),
        "탈퇴": ("https://help.miricanvas.com/hc/ko/articles/360036090071",
               "https://help.miricanvas.com/hc/ko/articles/360036090071"),
    },
    "n8n.io": {
        "해지": ("https://app.n8n.cloud/manage",
               "https://support.n8n.io/article/i-want-to-cancel-my-subscription"),
        "탈퇴": ("https://app.n8n.cloud/manage",
               "https://support.n8n.io/article/how-do-i-delete-my-n-8-n-account"),
    },
    "nate.com": {
        "탈퇴": ("https://helpdesk.nate.com/web/member/out",
               "https://helpdesk.nate.com/web/member/info"),
        "보안": ("https://helpdesk.nate.com/web/member/password",
               "https://helpdesk.nate.com/web/member/info"),
    },
    "naver.com": {
        "탈퇴": ("https://help.naver.com/service/5640/contents/1435?lang=ko",
               "https://help.naver.com/service/5640/contents/1435?lang=ko"),
        "보안": ("https://nid.naver.com/user2/help/myInfoV2?m=viewSecurity",
               "https://help.naver.com/service/5640/contents/18980?lang=ko"),
    },
    "netflix.com": {
        "해지": ("https://www.netflix.com/cancelplan",
               "https://help.netflix.com/en/node/407"),
        "보안": ("https://netflix.com/password",
               "https://help.netflix.com/en/node/365"),
    },
    "nexon.com": {
        "탈퇴": ("https://cs.nexon.com/helpBoard/popuphelpview/14057",
               "https://cs.nexon.com/helpBoard/popuphelpview/14057"),
    },
    "nike.com": {
        "탈퇴": ("https://www.nike.com/help/a/delete-nike-profile",
               "https://www.nike.com/help/a/delete-nike-profile"),
    },
    "nintendo.com": {
        "탈퇴": ("https://accounts.nintendo.com/withdraw/confirm",
               "https://en-americas-support.nintendo.com/app/answers/detail/a_id/15986/~/how-to-delete-a-nintendo-account"),
        "보안": ("https://accounts.nintendo.com/",
               "https://en-americas-support.nintendo.com/app/answers/detail/a_id/27496"),
    },
    "notion.so": {
        "탈퇴": ("https://www.notion.com/help/delete-your-account",
               "https://www.notion.com/help/delete-your-account"),
    },
    "onstove.com": {
        "탈퇴": ("https://support.onstove.com/hc/ko/articles/226740728",
               "https://support.onstove.com/hc/ko/articles/226740728"),
    },
    "openai.com": {
        "탈퇴": ("https://help.openai.com/en/articles/6378407-how-to-delete-your-account",
               "https://help.openai.com/en/articles/6378407-how-to-delete-your-account"),
    },
    "pixiv.net": {
        "탈퇴": ("https://www.pixiv.help/hc/en-us/articles/235792407-I-want-to-delete-my-pixiv-account",
               "https://www.pixiv.help/hc/en-us/articles/235792407-I-want-to-delete-my-pixiv-account"),
    },
    "plusdocs.com": {
        "해지": ("https://app.plusdocs.com/billing-portal",
               "https://guide.plusdocs.com/plus-ai/plus-ai-plans-and-billing"),
    },
    "pokemon.com": {
        "탈퇴": ("https://trainer.pokemon.com/profile/data-security",
               "https://support.pokemon.com/hc/en-us/articles/360000965826"),
        "보안": ("https://trainer.pokemon.com/profile/change-password",
               "https://support.pokemon.com/hc/en-us/articles/360002070073"),
    },
    "polar.com": {
        "탈퇴": ("https://account.polar.com/",
               "https://support.polar.com/en/support/tips/How_to_unregister_Polar_Account"),
    },
    "postype.com": {
        "해지": ("https://www.postype.com/membership",
               "https://help.postype.com/hc/ko/articles/900004046146"),
        "탈퇴": ("https://www.postype.com/account/settings",
               "https://help.postype.com/hc/ko/articles/360000117453"),
        "보안": ("https://www.postype.com/account/settings",
               "https://help.postype.com/hc/ko/articles/360006022773"),
    },
    "prezi.com": {
        "해지": ("https://prezi.com/settings/",
               "https://support.prezi.com/hc/en-us/articles/360003478894-How-to-manage-your-Prezi-subscription"),
        "탈퇴": ("https://prezi.com/settings/",
               "https://support.prezi.com/hc/en-us/articles/360003499213-Deleting-an-account"),
    },
    "readdle.com": {
        "해지": ("https://sparkmailapp.com/help/billing-subscription/how-to-cancel-the-subscription",
               "https://sparkmailapp.com/help/billing-subscription/how-to-cancel-the-subscription"),
        "탈퇴": ("https://sparkmailapp.com/help/privacy-data/remove-data-from-spark-request-data-copy-or-deletion",
               "https://sparkmailapp.com/help/privacy-data/remove-data-from-spark-request-data-copy-or-deletion"),
    },
    "riotgames.com": {
        "탈퇴": ("https://support.riotgames.com/en-us/riot/account/deleting-your-riot-account-and-all-your-data",
               "https://support.riotgames.com/en-us/riot/account/deleting-your-riot-account-and-all-your-data"),
        "보안": ("https://account.riotgames.com/",
               "https://support.riotgames.com/en-us/riot/account/multi-factor-authentication-how-to-faq"),
    },
    "runtastic.com": {
        "탈퇴": ("https://help.runtastic.com/hc/en-us/articles/200370082-Delete-Account",
               "https://help.runtastic.com/hc/en-us/articles/200370082-Delete-Account"),
    },
    "samsung.com": {
        "탈퇴": ("https://www.samsung.com/latin_en/support/apps-services/how-can-i-delete-my-samsung-account/",
               "https://www.samsung.com/latin_en/support/apps-services/how-can-i-delete-my-samsung-account/"),
    },
    "saramin.co.kr": {
        "탈퇴": ("https://www.saramin.co.kr/zf_user/help/help-word/view?idx=319",
               "https://www.saramin.co.kr/zf_user/help/help-word/view?idx=319"),
    },
    "sendanywhere.com": {
        "해지": ("https://send-anywhere.com/user/settings/#plan",
               "https://support.send-anywhere.com/hc/en-us/articles/16261798546585"),
        "탈퇴": ("https://send-anywhere.com/user/settings/#account",
               "https://support.send-anywhere.com/hc/en-us/articles/115003885394"),
    },
    "simplenote.com": {
        "탈퇴": ("https://app.simplenote.com/",
               "https://simplenote.com/help/"),
    },
    "snapchat.com": {
        "탈퇴": ("https://accounts.snapchat.com/",
               "https://help.snapchat.com/hc/en-us/articles/7012328360596-How-do-I-deactivate-or-delete-my-Snapchat-account"),
    },
    "snyk.io": {
        "탈퇴": ("https://support.snyk.io/hc/en-us/articles/360000921998-How-can-I-delete-my-account",
               "https://support.snyk.io/hc/en-us/articles/360000921998-How-can-I-delete-my-account"),
    },
    "soundcloud.com": {
        "해지": ("https://soundcloud.com/you/subscriptions",
               "https://help.soundcloud.com/hc/en-us/articles/360056011234-Cancel-your-subscription"),
        "탈퇴": ("https://help.soundcloud.com/hc/en-us/articles/115003563188-How-can-I-delete-my-account",
               "https://help.soundcloud.com/hc/en-us/articles/115003563188-How-can-I-delete-my-account"),
    },
    "spotify.com": {
        "해지": ("https://www.spotify.com/account/subscription/manage/",
               "https://support.spotify.com/us/article/cancel-premium/"),
        "탈퇴": ("https://www.spotify.com/account/close/",
               "https://support.spotify.com/us/article/close-account/"),
        "보안": ("https://www.spotify.com/account/login-methods-v2/password/",
               "https://support.spotify.com/us/article/change-password/"),
    },
    "tinkercad.com": {
        "탈퇴": ("https://www.tinkercad.com/help-center/deleting-an-account-and-all-data",
               "https://www.tinkercad.com/help-center/deleting-an-account-and-all-data"),
    },
    "toss.im": {
        "탈퇴": ("https://support.toss.im/faq/218",
               "https://support.toss.im/faq/218?from=12&page=1"),
    },
    "twitch.tv": {
        "보안": ("https://www.twitch.tv/settings/security",
               "https://help.twitch.tv/s/article/authy-faq"),
    },
    "upstage.ai": {
        "탈퇴": ("https://get.support.upstage.ai/servicedesk/customer/portal/7/create/10089",
               "https://get.support.upstage.ai/servicedesk/customer/portal/7/create/10089"),
    },
    "viarail.ca": {
        "탈퇴": ("https://www.viarail.ca/en/plan/faq/booking-and-ticket-purchase/how-do-i-delete-my-account",
               "https://www.viarail.ca/en/plan/faq/booking-and-ticket-purchase/how-do-i-delete-my-account"),
    },
    "watcha.com": {
        "탈퇴": ("https://help.watcha.com/hc/ko/articles/900002408246",
               "https://help.watcha.com/hc/ko/articles/900002408246"),
    },
    "wish.com": {
        "탈퇴": ("https://cs-help.wish.com/hc/en-us/articles/360028156211-Deactivate-or-Deleting-Wish-Accounts",
               "https://cs-help.wish.com/hc/en-us/articles/360028156211-Deactivate-or-Deleting-Wish-Accounts"),
    },
    "wishket.com": {
        "탈퇴": ("https://www.wishket.com/customer-support/10/44/149",
               "https://www.wishket.com/customer-support/10/44/149"),
    },
    "x.com": {
        "탈퇴": ("https://help.x.com/managing-your-account/how-to-deactivate-x-account",
               "https://help.x.com/managing-your-account/how-to-deactivate-x-account"),
    },
    "yorba.co": {
        "탈퇴": ("https://yorba.app/settings",
               "https://helpcenter.yorba.co/en/articles/7843637-delete-your-yorba-account"),
    },
    "youtube.com": {
        "해지": ("https://www.youtube.com/paid_memberships",
               "https://support.google.com/youtube/answer/6308278?hl=ko"),
        "보안": ("https://myaccount.google.com/signinoptions/two-step-verification",
               "https://support.google.com/accounts/answer/185839?hl=ko"),
    },
    "zoom.us": {
        "해지": ("https://zoom.us/billing",
               "https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0066687"),
        "탈퇴": ("https://zoom.us/account",
               "https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0067341"),
    },
}
# 같은 서비스의 다른 도메인 (analysis.py가 둘 중 하나로 묶는다)
CURATED["kakaocorp.com"] = CURATED["kakao.com"]
CURATED["behance.com"] = CURATED["behance.net"]
CURATED["smilegate.com"] = CURATED["onstove.com"]
CURATED["navercorp.com"] = CURATED["naver.com"]

# 기준 도메인 밖에 있는 같은 회사의 페이지 (검색 결과 검증에도 쓴다)
ALLOWED_DOMAINS: Dict[str, List[str]] = {
    "anthropic.com": ["claude.ai", "claude.com"],
    "aws.amazon.com": ["console.aws.amazon.com"],
    "behance.com": ["behance.net"],
    "clipstudio.net": ["clip-studio.com"],
    "coursera.org": ["coursera.support"],
    "daum.net": ["kakao.com"],
    "google.com": ["youtube.com"],
    "huggingface.co": ["hf.co"],
    "kakaocorp.com": ["kakao.com"],
    "n8n.io": ["n8n.cloud"],
    "navercorp.com": ["naver.com"],
    "notion.so": ["notion.com"],
    "pixiv.net": ["pixiv.help"],
    "readdle.com": ["sparkmailapp.com"],
    "sendanywhere.com": ["send-anywhere.com"],
    "smilegate.com": ["onstove.com"],
    "yorba.co": ["yorba.app"],
    "youtube.com": ["google.com"],
}

_LABELS = {
    "해지": ("해지하러 가기", "해지 방법 찾아보기"),
    "탈퇴": ("탈퇴하러 가기", "탈퇴 방법 찾아보기"),
    "보안": ("보안 설정 열기", "보안 설정 방법 찾아보기"),
}


def base_domain(account_id: str) -> str:
    account_id = (account_id or "").strip().lower()
    if account_id.startswith("apps.apple.com/"):
        return "apple.com"
    return account_id


def _host_allowed(host: str, domain: str) -> bool:
    allowed = [domain] + ALLOWED_DOMAINS.get(domain, [])
    return any(host == d or host.endswith("." + d) for d in allowed)


def is_valid(url: str, domain: str) -> bool:
    """https, 사용자 정보·포트 없음, IP 아닌 호스트가 그 계정의 도메인(하위 도메인 포함) 또는 허용 도메인일 때만."""
    try:
        parts = urlsplit(url or "")
        if parts.scheme != "https" or parts.username is not None or parts.password is not None:
            return False
        # 포트는 빈 값(`host:`)까지 거절한다. IPv6 호스트도 콜론이 있지만 어차피 IP라 거절된다
        if ":" in parts.netloc:
            return False
    except ValueError:
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    return _host_allowed(host, base_domain(domain))


def fallback_link(service: str, action: str) -> Dict[str, str]:
    return {"url": "https://www.google.com/search?q=" + quote_plus(f"{service} {action} 방법"), "source": "fallback"}


def _cached(domain: str, action: str) -> Optional[str]:
    try:
        row = db.get_action_link(domain, action)
    except Exception as e:  # 캐시가 안 되면 없는 것으로 본다
        print(f"[links] 캐시 조회 실패 {domain} {action}: {e!r}")
        return None
    if not row or not is_valid(row["url"], domain):
        return None
    if datetime.fromisoformat(row["found_at"]) < datetime.now() - timedelta(days=CACHE_DAYS):
        return None
    return row["url"]


def resolve_offline(account: Dict[str, Any], action: str, use_cache: bool = True) -> Dict[str, str]:
    """네트워크 없이 고른다: 큐레이션 → (캐시) → 구글 검색 대체."""
    account_id = account.get("id") or ""
    domain = base_domain(account_id)
    if action == "해지" and account_id.lower().startswith("apps.apple.com/"):
        return {"url": APPLE_SUBSCRIPTIONS, "source": "curated"}
    curated = CURATED.get(domain, {}).get(action)
    if curated:
        return {"url": curated[0], "source": "curated"}
    if use_cache:
        url = _cached(domain, action)
        if url:
            return {"url": url, "source": "cache"}
    return fallback_link(account.get("service") or domain, action)


def label(action: str, source: str) -> str:
    found, search = _LABELS[action]
    return search if source == "fallback" else found


# --- 리포트 데이터에 붙이기 ----------------------------------------------------------

_GOOGLE_PAGE_RE = re.compile(r"myaccount\.google\.com/([a-z-]+)")


def needed_actions(account: Dict[str, Any]) -> List[str]:
    """리포트가 버튼을 다는 조치: 구독 중이면 해지, 안 쓰는 계정이면 해지(구독 해지 권장) 또는 탈퇴."""
    actions: List[str] = []
    if account.get("subscription") and account["subscription"].get("status") in ACTIVE:
        actions.append("해지")
    if account.get("unused"):
        actions.append("해지" if account.get("recommended") == "구독 해지" else "탈퇴")
    return list(dict.fromkeys(actions))


def security_link(account: Dict[str, Any], insight: Dict[str, Any], use_cache: bool = True) -> Dict[str, str]:
    """보안 알림 하나의 링크. 안내 문구가 Google 계정의 특정 페이지를 가리키면 그 페이지로."""
    if base_domain(account.get("id") or "") == "google.com":
        match = _GOOGLE_PAGE_RE.search(insight.get("advice") or "")
        if match:
            return {"url": f"https://myaccount.google.com/{match.group(1)}", "source": "curated"}
    return resolve_offline(account, "보안", use_cache=use_cache)


def attach_offline(report: Dict[str, Any]) -> None:
    """네트워크 없이 모든 칸을 채운다. 이미 정해진 칸은 그대로 둔다. 어떤 실패도 리포트를 깨지 않는다."""
    for account in report.get("accounts") or []:
        service = account.get("service") or account.get("id") or ""
        actions = account.setdefault("actions", {})
        for action in needed_actions(account):
            if action not in actions:
                try:
                    actions[action] = resolve_offline(account, action)
                except Exception as e:
                    print(f"[links] {account.get('id')} {action}: {e!r}")
                    actions[action] = fallback_link(service, action)
        for insight in account.get("insights") or []:
            if insight.get("kind") == "보안" and "action" not in insight:
                try:
                    insight["action"] = security_link(account, insight)
                except Exception as e:
                    print(f"[links] {account.get('id')} 보안: {e!r}")
                    insight["action"] = fallback_link(service, "보안")


# --- 웹 검색 (결제한 리포트에서만) -----------------------------------------------------

_URL_RE = re.compile(r"https://[^\s)\]>\"'<]+")
_QUERY_WHAT = {"해지": "구독 해지(취소) 페이지", "탈퇴": "회원 탈퇴(계정 삭제) 페이지", "보안": "비밀번호·로그인 보안 설정 페이지"}


def _candidates(body: Dict[str, Any]) -> List[str]:
    urls: List[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            urls += [u.rstrip(".,;:!?)") for u in _URL_RE.findall(part.get("text") or "")]
            urls += [a.get("url") or "" for a in part.get("annotations") or [] if isinstance(a, dict)]
    return urls


def search_link(service: str, domain: str, action: str, api_key: str) -> Optional[str]:
    """OpenAI 웹 검색으로 그 조치 페이지 URL을 찾는다. 검증을 통과한 첫 URL, 없으면 None. 예외를 던지지 않는다.
    질의에는 서비스 이름·도메인·조치만 넣는다 (사용자 메일 주소·메일 내용은 보내지 않는다)."""
    query = (f"{service}({domain}) 계정의 {_QUERY_WHAT[action]} 공식 URL 하나만 답해. "
             f"{domain}의 공식 페이지나 공식 도움말에 있는 주소만. 모르면 NONE이라고만 답해.")
    try:
        response = httpx.post(
            OPENAI_RESPONSES_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": os.getenv("OPENAI_MODEL", "gpt-4.1"), "tools": [{"type": "web_search"}], "input": query},
            timeout=SEARCH_TIMEOUT,
        )
        response.raise_for_status()
        candidates = _candidates(response.json())
    except Exception as e:
        print(f"[links] 검색 실패 {domain} {action}: {type(e).__name__}")
        return None
    return next((u for u in candidates if is_valid(u, domain)), None)


def upgrade_report(report: Dict[str, Any], api_key: Optional[str] = None) -> int:
    """구글 검색 대체 링크로 채운 칸만 웹 검색으로 다시 찾는다. 바꾼 칸 수를 돌려준다."""
    api_key = (os.getenv("OPENAI_API_KEY", "") if api_key is None else api_key).strip()
    if not api_key:
        return 0
    slots: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], str]]] = {}
    services: Dict[Tuple[str, str], str] = {}
    for account in report.get("accounts") or []:
        domain = base_domain(account.get("id") or "")
        if not domain or "/" in domain:
            continue
        for action, link in (account.get("actions") or {}).items():
            if link.get("source") == "fallback":
                slots.setdefault((domain, action), []).append((account["actions"], action))
                services[(domain, action)] = account.get("service") or domain
        for insight in account.get("insights") or []:
            if (insight.get("action") or {}).get("source") == "fallback":
                slots.setdefault((domain, "보안"), []).append((insight, "action"))
                services[(domain, "보안")] = account.get("service") or domain
    keys = list(slots)[:MAX_SEARCHES]
    if not keys:
        return 0
    pool = ThreadPoolExecutor(max_workers=SEARCH_WORKERS, thread_name_prefix="links")
    futures = {pool.submit(search_link, services[k], k[0], k[1], api_key): k for k in keys}
    done, _ = wait(futures, timeout=SEARCH_DEADLINE)
    pool.shutdown(wait=False, cancel_futures=True)
    changed = 0
    for future in done:
        url = future.result()
        if not url:
            continue
        domain, action = futures[future]
        for holder, key in slots[(domain, action)]:
            holder[key] = {"url": url, "source": "search"}
            changed += 1
        try:
            db.put_action_link(domain, action, url, "search")
        except Exception as e:
            print(f"[links] 캐시 저장 실패 {domain} {action}: {e!r}")
    return changed
