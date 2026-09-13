import pytest
from datetime import date

from src.format_telegram import TOPIC_CAP, format_daily_digest
from src.models import Item


def item(category, title, *, author="출처", url=None, **extra):
    return Item(source_id="s", category=category, title=title,
                url=url or f"https://ex.com/{abs(hash(title)) % 10**6}",
                natural_key=title[:8], key_prefix="k", author=author, extra=extra)


DAY = date(2026, 9, 11)  # 금요일


def test_header_has_date_and_weekday():
    msgs = format_daily_digest([item("news", "뉴스")], DAY, title="라벤더")
    assert msgs[0].startswith("<b>[라벤더] 2026년 9월 11일 (금)</b>")


def test_empty_input_produces_no_message():
    assert format_daily_digest([], DAY) == []


def test_sections_ordered_by_reader_value():
    items = [item("news", "뉴스"), item("bid", "입찰"),
             item("committee", "창원시 도시계획위원회 위원 공개모집")]
    body = "\n".join(format_daily_digest(items, DAY))
    assert body.index("위원 모집") < body.index("입찰공고") < body.index("뉴스")


def test_item_without_metadata_has_no_empty_meta_line():
    body = "\n".join(format_daily_digest([item("news", "제목만")], DAY))
    assert " · " not in body


def test_news_grouped_by_topic_not_outlet():
    items = [item("news", "국토부 시행령 개정안 입법예고", author="국토일보"),
             item("news", "현대건설 1조원대 재개발 수주", author="한국건설신문")]
    body = "\n".join(format_daily_digest(items, DAY))
    assert "&lt;정책·제도&gt;" in body and "&lt;수주·계약&gt;" in body
    assert "&lt;국토일보&gt;" not in body  # 그룹 헤더는 분야, 매체는 항목에


def test_news_keeps_outlet_attribution_on_each_item():
    body = "\n".join(format_daily_digest([item("news", "제목", author="국토일보")], DAY))
    assert "국토일보" in body


def test_per_topic_cap_and_omitted_count():
    items = [item("news", f"{i} 국토부 개정 고시", author="국토일보")
             for i in range(TOPIC_CAP + 3)]
    body = "\n".join(format_daily_digest(items, DAY))
    assert body.count("▶️") == TOPIC_CAP
    assert "외 3건" in body


def test_bid_section_cap():
    from src.format_telegram import DIGEST_SECTIONS
    cap = next(c for k, _, c in DIGEST_SECTIONS if k == "bid")
    items = [item("bid", f"공사{i}") for i in range(cap + 5)]
    body = "\n".join(format_daily_digest(items, DAY))
    assert body.count("▶️") == cap and "외 5건" in body


def test_committee_section_is_uncapped():
    """위원 모집만은 상한을 두지 않는다 — 상한이 이 구획을 자르면 존재 이유가 없다."""
    items = [item("committee", f"제{i}기 기술자문위원회 위원 공개모집") for i in range(40)]
    body = "\n".join(format_daily_digest(items, DAY))
    assert body.count("▶️") == 40 and "외 " not in body


def test_title_is_the_link_and_html_is_escaped():
    items = [item("news", "제목 <b>&amp;</b> 특수문자", url="https://ex.com/a?x=1&y=2")]
    body = "\n".join(format_daily_digest(items, DAY))
    assert '<a href="https://ex.com/a?x=1&amp;y=2">' in body
    assert "&lt;b&gt;" in body


def test_splits_across_messages_within_telegram_limit():
    items = [item("committee", f"아주 긴 기술자문위원 공개모집 공고 제목입니다 {i}" * 4)
             for i in range(200)]
    msgs = format_daily_digest(items, DAY)
    assert len(msgs) > 1
    assert all(len(m) <= 4096 for m in msgs)


def test_site_link_appended_only_when_configured():
    with_url = "\n".join(format_daily_digest([item("news", "가")], DAY, "https://site.example"))
    without = "\n".join(format_daily_digest([item("news", "가")], DAY, ""))
    assert "전체 공고·아카이브" in with_url
    assert "전체 공고·아카이브" not in without


def test_pathological_url_falls_back_to_plain_title():
    """URL 하나가 한도를 넘어도 메시지가 4096자를 넘지 않아야 한다."""
    from src.format_telegram import SAFE_LIMIT
    huge = "https://ex.com/?q=" + "x" * (SAFE_LIMIT + 500)
    msgs = format_daily_digest([item("news", "정상 제목", url=huge)], DAY)
    body = "\n".join(msgs)
    assert all(len(m) <= 4096 for m in msgs)
    assert "정상 제목" in body and huge not in body


@pytest.mark.parametrize("raw,expected", [
    ("1,754,181,818원", "17.5억"),
    ("2,000,000,000원", "20억"),
    ("132,672,027원", "1.3억"),
    ("7,240,909원", "724만"),
    ("980원", "980원"),
    ("금액미상", "금액미상"),
])
def test_compact_amount(raw, expected):
    from src.format_telegram import compact_amount
    assert compact_amount(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("2026-09-21 18:00", "9/21 18:00"),
    ("2026-10-14", "10/14"),
    ("2026.09.05", "9/5"),
    ("미정", "미정"),
])
def test_compact_deadline(raw, expected):
    from src.format_telegram import compact_deadline
    assert compact_deadline(raw) == expected


def test_bid_meta_uses_compact_forms():
    items = [item("bid", "OO공사", org="부산청",
                  deadline="2026-09-20 17:00", amount="1,234,000,000원")]
    body = "\n".join(format_daily_digest(items, DAY))
    assert "부산청 · 마감 9/20 17:00 · 12.3억" in body


def test_long_title_truncated_to_digest_width():
    from src.format_telegram import DIGEST_TITLE_MAX
    body = "\n".join(format_daily_digest([item("news", "가" * 300)], DAY))
    assert "…" in body and "가" * (DIGEST_TITLE_MAX + 1) not in body


def test_one_outlet_cannot_dominate_news():
    """발행량 많은 매체가 뉴스 지면을 독점하지 않아야 한다.

    같은 분야에 한 매체가 20건, 다른 매체가 1건이어도 소수 매체가 잘려
    나가서는 안 된다. 매체 상한만으로는 부족하고 라운드로빈이 필요하다.
    """
    from src.format_telegram import PER_SOURCE_CAP
    items = ([item("news", f"국토부 고시 {i}", author="건설타임즈") for i in range(20)]
             + [item("news", "국토부 고시 A", author="국토일보")])
    body = "\n".join(format_daily_digest(items, DAY))
    assert "국토일보" in body
    assert body.count("건설타임즈") <= PER_SOURCE_CAP


def test_balance_reports_dropped_count():
    from src.format_telegram import PER_SOURCE_CAP, _balance_by_source
    items = [item("news", f"기사{i}", author="A") for i in range(PER_SOURCE_CAP + 4)]
    kept, dropped = _balance_by_source(items, PER_SOURCE_CAP)
    assert len(kept) == PER_SOURCE_CAP and dropped == 4


def test_balance_interleaves_outlets():
    from src.format_telegram import _balance_by_source
    items = ([item("news", f"A{i}", author="A") for i in range(3)]
             + [item("news", "B0", author="B")])
    kept, _ = _balance_by_source(items, 3)
    assert [i.author for i in kept][:2] == ["A", "B"]


def test_bids_below_floor_are_dropped():
    from src.format_telegram import BID_MIN_AMOUNT
    small = item("bid", "소액 용역", amount=f"{BID_MIN_AMOUNT - 1:,}원")
    big = item("bid", "대형 공사", amount=f"{BID_MIN_AMOUNT:,}원")
    body = "\n".join(format_daily_digest([small, big], DAY))
    assert "대형 공사" in body and "소액 용역" not in body


def test_bids_without_amount_are_kept():
    """금액을 모르면 작다고 단정할 수 없으므로 남긴다."""
    body = "\n".join(format_daily_digest([item("bid", "금액 미상 공고")], DAY))
    assert "금액 미상 공고" in body


def test_bids_sorted_by_amount_desc():
    items = [item("bid", "중간", amount="5,000,000,000원"),
             item("bid", "최대", amount="90,000,000,000원"),
             item("bid", "최소", amount="1,000,000,000원")]
    body = "\n".join(format_daily_digest(items, DAY))
    assert body.index("최대") < body.index("중간") < body.index("최소")


def test_bid_section_header_states_the_floor():
    from src.format_telegram import BID_MIN_LABEL
    body = "\n".join(format_daily_digest([item("bid", "공사", amount="20,000,000,000원")], DAY))
    assert f"추정가 {BID_MIN_LABEL} 이상" in body


def test_parse_won():
    from src.format_telegram import parse_won
    assert parse_won("1,234,000,000원") == 1234000000
    assert parse_won("미상") is None
    assert parse_won("") is None


# ── 의도별 구획 분리 (실측 2026-09: 위원 모집이 세미나에 묻혔다) ──────────────

from src.format_telegram import section_of  # noqa: E402


def _c(title, source="aik_news"):
    return Item(source_id=source, category="committee", title=title,
                url=f"https://ex.com/{abs(hash(title)) % 9999}", author="대한건축학회")


def test_committee_board_is_split_by_intent():
    assert section_of(_c("창원시 도시계획위원회 위원 공개모집 안내")) == "recruit"
    assert section_of(_c("『가납초 학교복합시설 구축사업 』 건축설계공모")) == "competition"
    assert section_of(_c("제10회 스마트건설교류회 세미나 개최")) == "association"


def test_other_categories_keep_their_section():
    assert section_of(Item(source_id="ikld", category="news", title="x", url="u")) == "news"
    assert section_of(Item(source_id="g2b", category="bid", title="x", url="u")) == "bid"


def test_recruitment_leads_the_digest():
    items = [
        _c("제10회 스마트건설교류회 세미나 개최 (10/14)"),
        _c("『가납초 학교복합시설 구축사업 』 건축설계공모"),
        _c("창원시 도시계획위원회 위원 공개모집 안내"),
    ]
    text = "\n".join(format_daily_digest(items, date(2026, 9, 13), ""))
    assert text.index("위원 모집") < text.index("설계공모") < text.index("협회 소식")


def test_roster_never_reaches_the_digest():
    """명단은 지원할 수 없는 정보이고 사람 이름이 섞인다."""
    items = [
        _c("'가덕도신공항 부지조성공사' 일괄입찰 설계심의위원 명단"),
        _c("창원시 도시계획위원회 위원 공개모집 안내"),
    ]
    text = "\n".join(format_daily_digest(items, date(2026, 9, 13), ""))
    assert "명단" not in text
    assert "창원시" in text


def test_a_digest_of_only_rosters_is_not_sent():
    items = [_c("제3기 건설엔지니어링 종합심사낙찰제 심사위원회 위원 명단(2026.9.4 기준)")]
    assert format_daily_digest(items, date(2026, 9, 13), "") == []
