"""공고의 의도 분류 — 지금 지원할 수 있는 것과 이미 끝난 것을 가른다.

실측(2026-09)이 이 모듈을 만든 이유다. 9일간 들어온 진짜 위원 모집 6건이
전부 발송되지 않고, 대신 심포지엄·방송 출연 모집·'설계심의위원 명단'이 나갔다.
수집은 되고 있었다. 고르는 기준이 시간순뿐이었던 것이 문제였다.

분류는 제목만 본다. 상세 페이지를 열지 않는다는 원칙을 지킨다.
순서가 곧 우선순위다 — 명단을 먼저 걸러야 '심의위원 명단'이 모집으로 새지 않는다.
"""
from __future__ import annotations

import re

# 지금 지원할 수 있는 것. 이 플랫폼의 존재 이유다.
RECRUIT = "recruit"
# 위촉이 끝난 결과 발표. 지원할 수 없고, 사람 이름이 들어 있다.
# 게시하지 않되 버리지도 않는다 — 기수·임기를 읽어낼 유일한 단서다.
ROSTER = "roster"
# 설계공모·현상설계. 위원 모집이 아니라 일감이다. 섞이면 둘 다 안 읽힌다.
COMPETITION = "competition"
# 세미나·교육·행사.
EVENT = "event"
OTHER = "other"

# 위에서부터 먼저 맞는 것이 이긴다.
_RULES: tuple[tuple[str, re.Pattern], ...] = (
    # 명단이 가장 먼저다. "설계심의위원 명단"은 '위원'을 포함하지만 모집이 아니다.
    # '명단 등재 희망자 모집'은 명단 공개가 아니라 모집이다 — 실측에서 법원행정처
    # 감정인 공고가 여기 걸려 잘못 배제됐다. 등재·등록이 뒤따르면 명단이 아니다.
    (ROSTER, re.compile(
        r"명단(?!\s*(등재|등록))|위촉\s*결과|선정\s*결과|심사\s*결과|구성\s*현황"
        r"|위원회\s*구성(?!\s*원)|선정\s*공고|결과\s*발표|최종\s*선정")),
    # 위원 모집. '위원'이나 '심사단' 같은 주체어 + 모집·공모·등록류가 함께 있어야 한다.
    # 주체어에 맨 '후보자'는 넣지 않는다 — "정부포상 후보자 공모"까지 위원 모집으로
    # 끌려온다. 위원·심사단처럼 자리를 가리키는 말이 있어야 한다.
    (RECRUIT, re.compile(
        r"(위원|심사단|평가단|점검단|자문단|감정인|심의관|전문가\s*풀)"
        r"[^\n]{0,20}?"
        r"(공개\s*모집|공모|모집|위촉|추천|등록|선발|초빙|풀\s*등록|인력\s*풀)")),
    # 설계공모·현상설계 — 일감이지 위원 자리가 아니다.
    (COMPETITION, re.compile(r"설계\s*공모|현상\s*설계|제안\s*공모|아이디어\s*공모|디자인\s*공모")),
    (EVENT, re.compile(
        r"세미나|심포지엄|포럼|컨퍼런스|콘퍼런스|학술대회|워크숍|워크샵"
        r"|교육\s*과정|강좌|설명회|간담회|전시회|공모전|시상|수상|개최\s*안내")),
)


def classify_intent(title: str) -> str:
    """제목에서 공고의 의도를 고른다. 해당 없으면 'other'."""
    if not title:
        return OTHER
    text = title.strip()
    for intent, pattern in _RULES:
        if pattern.search(text):
            return intent
    return OTHER


# 다이제스트 선별 우선순위 — 상한에 걸렸을 때 무엇을 먼저 살릴 것인가.
# 실측에서 상한이 세미나를 살리고 위원 모집을 버렸다.
_RANK = {RECRUIT: 0, COMPETITION: 1, OTHER: 2, EVENT: 3, ROSTER: 9}


def priority(title: str) -> int:
    """낮을수록 먼저 실린다."""
    return _RANK.get(classify_intent(title), 2)


def is_postable(title: str) -> bool:
    """게시해도 되는가. 명단·결과 공고는 지원할 수 없고 개인정보가 섞인다."""
    return classify_intent(title) != ROSTER
