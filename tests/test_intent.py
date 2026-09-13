"""공고 의도 분류 — 전부 실측 제목이다 (state/seen.db, 2026-09).

이 모듈이 생긴 이유가 회귀 테스트의 내용이기도 하다: 아래 recruit 6건은
실제로 수집됐으나 한 건도 발송되지 않았고, 대신 세미나·방송 출연 모집과
'설계심의위원 명단'이 발송됐다.
"""
import pytest

from src.intent import (COMPETITION, EVENT, OTHER, RECRUIT, ROSTER,
                        classify_intent, is_postable, priority)

# 실제로 놓쳤던 위원 모집 공고들
MISSED = [
    "경기주택도시공사 제15기 기술자문위원회 위원 후보자 등록 안내(~9/30)",
    "[건축계소식] [법원행정처]2027년도 감정인 명단 등재 희망자 모집 안내",
    "[건축계소식] [국가철도공단]자산개발위원회 외부위원 후보자 등록 안내",
    "[건축계소식] [안양시]「안양시 특별건축구역 지정·운영 지침 수립 용역」 제안서 평가위원(후보자) 공개모집 안내",
    "[건축계소식] [창원시]창원시 도시계획위원회 위원 공개모집 안내",
    "'26년도 공공기관 안전관리등급 심사단 예비 공모 알림",
]

# 실제로 발송됐던 것들 — 위원 모집보다 앞설 수 없어야 한다
SENT_INSTEAD = [
    ("[서울시립대학교 서울학연구소] 2026 서울학공동심포지엄 개최 안내", EVENT),
    ("[스마트건설교류회] 제10회 스마트건설교류회 세미나 개최 (10/14)", EVENT),
    ("'가덕도신공항 부지조성공사' 일괄입찰 설계심의위원 명단", ROSTER),
]


@pytest.mark.parametrize("title", MISSED)
def test_real_recruitment_notices_are_recognized(title):
    assert classify_intent(title) == RECRUIT


@pytest.mark.parametrize("title,expected", SENT_INSTEAD)
def test_what_was_sent_instead_is_demoted(title, expected):
    assert classify_intent(title) == expected
    assert priority(title) > priority(MISSED[0])


def test_roster_is_not_postable():
    """명단은 지원할 수 없고 사람 이름이 들어 있다."""
    assert not is_postable("제3기 건설엔지니어링 종합심사낙찰제 심사위원회 위원 명단(2026.9.4 기준)")
    assert not is_postable("'가덕도신공항 부지조성공사' 일괄입찰 설계심의위원 명단")


def test_roster_word_does_not_swallow_a_real_recruitment():
    """'명단 등재 희망자 모집'은 명단 공개가 아니라 모집이다 — 실제로 놓쳤던 경계."""
    title = "2027년도 감정인 명단 등재 희망자 모집 안내"
    assert classify_intent(title) == RECRUIT
    assert is_postable(title)


def test_award_nomination_is_not_a_committee_seat():
    """'후보자 공모'만으로 위원 모집이 되면 포상 공모까지 끌려온다."""
    assert classify_intent("2026년 대한민국 주거복지대전 정부포상 후보자 공모") == OTHER


def test_design_competition_is_work_not_a_seat():
    for title in ("『가납초 학교복합시설 구축사업 』 건축설계공모",
                  "인덕원~동탄 복선전철 솔빛나루역사(가칭) 신축 제안설계공모 안내",
                  "[건축계소식] 당진성모병원 신축공사를 위한 건축설계공모"):
        assert classify_intent(title) == COMPETITION


def test_unrelated_recruitment_does_not_outrank_committee_seats():
    """'[모집] MBN 방송 무료 출연 기업 모집'이 위원 모집을 밀어냈었다."""
    noise = "[모집] MBN 방송 무료 출연 건축·건설·인테리어 기업 모집"
    assert classify_intent(noise) == OTHER
    assert priority(noise) > priority("창원시 도시계획위원회 위원 공개모집 안내")


def test_empty_title_is_safe():
    assert classify_intent("") == OTHER
    assert is_postable("")
