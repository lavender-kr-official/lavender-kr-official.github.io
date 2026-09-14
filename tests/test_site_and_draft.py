from datetime import datetime, timedelta, timezone

from src.blog_draft import build_draft
from src.config import SiteConfig
from src.dedup import SeenStore
from src.models import Item
from src.site_build import build_data

KST = timezone(timedelta(hours=9))


def seed(store: SeenStore) -> None:
    now_kst = datetime.now(KST)
    items = [
        Item(source_id="ikld", category="news", title="건설 뉴스 A",
             url="https://ex.com/n1", natural_key="1", key_prefix="rss:ikld",
             author="국토일보"),
        Item(source_id="g2b_construction", category="bid", title="OO대교 공사",
             url="https://ex.com/b1", natural_key="b1", key_prefix="g2b",
             extra={"org": "부산시", "deadline": (now_kst + timedelta(days=5)).strftime("%Y-%m-%d 17:00")}),
        Item(source_id="g2b_construction", category="bid", title="지난 공사",
             url="https://ex.com/b2", natural_key="b2", key_prefix="g2b",
             extra={"deadline": (now_kst - timedelta(days=3)).strftime("%Y-%m-%d 17:00")}),
        Item(source_id="ksce_external", category="committee",
             title=f"평가위원 모집 (~{(now_kst + timedelta(days=10)).strftime('%Y.%m.%d')})",
             url="https://ex.com/c1", natural_key="c1", key_prefix="board:ksce"),
        Item(source_id="ksce_external", category="committee", title="마감 미상 위원 모집",
             url="https://ex.com/c2", natural_key="c2", key_prefix="board:ksce"),
        Item(source_id="yt1", category="youtube", title="교량 시공 영상",
             url="https://youtube.com/watch?v=abc", natural_key="abc", key_prefix="yt",
             extra={"thumbnail": "https://img.youtube.com/vi/abc/mqdefault.jpg",
                    "channel": "토목채널"}),
    ]
    for it in items:
        store.mark_seen(it)
        store.mark_posted(it.dedup_key, 1)


def test_build_data_filters_expired(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    seed(store)
    data = build_data(store)
    bid_titles = [r["title"] for r in data["bids"]]
    assert "OO대교 공사" in bid_titles
    assert "지난 공사" not in bid_titles  # 마감 지난 입찰 제외
    committee_titles = [r["title"] for r in data["committees"]]
    assert any("평가위원 모집" in t for t in committee_titles)
    assert "마감 미상 위원 모집" in committee_titles  # 30일 이내 → 배지용으로 유지
    unknown = next(r for r in data["committees"] if r["title"] == "마감 미상 위원 모집")
    assert unknown["deadline"] is None
    # 마감 있는 항목이 미상보다 앞에 정렬
    assert data["committees"][0]["deadline"] is not None
    assert len(data["videos"]) == 1 and data["videos"][0]["thumbnail"]
    assert len(data["news"]) == 1
    store.close()


def test_build_draft_no_body_copy(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    seed(store)
    day = datetime.now(KST).date()
    content = build_draft(store, day, use_llm=False, site_url="https://ex.github.io/cloud")
    assert "제목 후보" in content
    assert "한 줄 해설" in content          # 사용자 작성 빈칸
    assert "OO대교 공사" in content
    assert "바로가기" in content
    assert "t.me" not in content            # 텔레그램 링크 미삽입 원칙
    store.close()


def test_build_draft_empty_day(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    day = datetime.now(KST).date()
    content = build_draft(store, day, use_llm=False)
    assert "게시된 콘텐츠가 없습니다" in content
    store.close()


def test_draft_falls_back_to_collected_when_nothing_posted(tmp_path):
    """텔레그램 미설정으로 게시분이 없어도 당일 수집분으로 초안이 만들어져야 함."""
    store = SeenStore(tmp_path / "seen.db")
    store.mark_seen(Item(source_id="ikld", category="news", title="수집만 된 뉴스",
                         url="https://ex.com/n9", natural_key="9", key_prefix="rss:ikld"))
    content = build_draft(store, datetime.now(KST).date(), use_llm=False)
    assert "수집만 된 뉴스" in content
    store.close()


def test_roster_never_reaches_the_public_site(tmp_path):
    """명단 공고는 사이트에도 실리면 안 된다 — 공개 색인되는 쪽이 더 위험하다.

    실측(2026-09-14): 텔레그램은 수집·표시 두 곳에서 막는데 사이트만 뚫려 있어
    '가덕도신공항 일괄입찰 설계심의위원 명단'이 공개 페이지에 살아 있었다.
    """
    from src.dedup import SeenStore
    from src.models import Item
    from src.site_build import build_data

    store = SeenStore(tmp_path / "t.db")
    store.mark_seen(Item(source_id="molit_notice", category="committee",
                         title="'가덕도신공항 부지조성공사' 일괄입찰 설계심의위원 명단",
                         url="https://ex.com/1", natural_key="1", key_prefix="rss:molit_notice"),
                    status="skipped")
    store.mark_seen(Item(source_id="kira_news", category="committee",
                         title="창원시 도시계획위원회 위원 공개모집 안내",
                         url="https://ex.com/2", natural_key="2", key_prefix="board:kira_news"),
                    status="posted")
    data = build_data(store)
    titles = [c["title"] for c in data["committees"]]
    assert titles == ["창원시 도시계획위원회 위원 공개모집 안내"]
    store.close()
