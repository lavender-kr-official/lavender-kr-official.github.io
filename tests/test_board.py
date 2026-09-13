import pytest

from src.collectors.board import parse_list_page
from src.config import SourceConfig, apply_keyword_filters
from src.httpio import decode_body

BOARD_HTML = """
<html><body>
<table class="board">
  <tr><th>번호</th><th>제목</th><th>작성일</th></tr>
  <tr>
    <td>2</td>
    <td class="subject"><a href="view.asp?bidx=102&page=1">[창원시] 평가위원 모집 공고</a></td>
    <td class="date">2026-08-28</td>
  </tr>
  <tr>
    <td>1</td>
    <td class="subject"><a href="view.asp?bidx=101">기술자문위원회 위원 공개모집</a></td>
    <td class="date">2026.08.27</td>
  </tr>
</table>
</body></html>
"""

ONCLICK_HTML = """
<table><tr>
  <td><a href="#" onclick="fnView('555'); return false;">심의위원 모집</a></td>
</tr></table>
"""


def board_source(**board_overrides) -> SourceConfig:
    board = {
        "list_url": "https://ex.or.kr/not/list.asp?page={page}",
        "row_selector": "table tr:has(a)",
        "url_base": "https://ex.or.kr/not/",
        "id_pattern": r"bidx=(\d+)",
        "fields": {
            "title": {"selector": "td.subject a", "attr": "text"},
            "link": {"selector": "td.subject a", "attr": "href"},
            "date": {"selector": "td.date", "attr": "text"},
        },
    }
    board.update(board_overrides)
    return SourceConfig(id="b1", name="게시판", type="board", category="committee",
                        options=board)


def test_parse_list_page_basic():
    items = parse_list_page(BOARD_HTML, board_source(), "https://ex.or.kr/not/list.asp")
    assert len(items) == 2
    first = items[0]
    assert first.title == "[창원시] 평가위원 모집 공고"
    assert first.url == "https://ex.or.kr/not/view.asp?bidx=102&page=1"
    assert first.natural_key == "102"
    assert first.dedup_key == "board:b1:102"
    assert first.published_at is not None
    # 점 구분 날짜도 파싱
    assert items[1].published_at is not None


def test_parse_onclick_link_template():
    src = board_source(fields={
        "title": {"selector": "a", "attr": "text"},
        "link": {"selector": "a", "attr": "onclick",
                 "pattern": r"fnView\('(\d+)'\)",
                 "template": "https://ex.or.kr/view.do?id={value}"},
    }, id_pattern=r"id=(\d+)")
    items = parse_list_page(ONCLICK_HTML, src, "https://ex.or.kr/")
    assert len(items) == 1
    assert items[0].url == "https://ex.or.kr/view.do?id=555"
    assert items[0].natural_key == "555"


def test_euc_kr_decode_roundtrip():
    text = "<html><meta charset='euc-kr'><body>한글 게시판 제목</body></html>"
    raw = text.encode("cp949")
    assert "한글 게시판 제목" in decode_body(raw, None, "auto")
    assert "한글 게시판 제목" in decode_body(raw, "euc-kr", "auto")
    assert "한글 게시판 제목" in decode_body(raw, None, "cp949")


def test_keyword_filters():
    filters = {"keyword_include": ["위원", "모집"], "keyword_exclude": ["채용"]}
    assert apply_keyword_filters("평가위원 모집 공고", filters)
    assert not apply_keyword_filters("직원 채용 위원회", filters)  # exclude 우선
    assert not apply_keyword_filters("일반 공지", filters)
    assert apply_keyword_filters("아무 제목", {})  # 필터 없으면 통과


def test_declared_utf8_with_stray_byte_stays_utf8():
    # 선언 인코딩이 맞고 일부 바이트만 깨진 경우 — 전체를 cp949로 오판하지 않고
    # 선언 인코딩 + replace로 처리해 나머지 본문이 살아남아야 함
    good = "<html><body>건설 뉴스 제목입니다</body></html>".encode("utf-8")
    boundary = len("<html><body>건설".encode("utf-8"))  # 문자 경계에 삽입
    decoded = decode_body(good[:boundary] + b"\xff" + good[boundary:], "utf-8", "auto")
    assert "건설" in decoded
    assert "뉴스 제목입니다" in decoded
    assert "�" in decoded  # 깨진 바이트만 대체 문자로


def test_collect_refuses_when_robots_disallows(monkeypatch):
    """robots.txt가 막으면 조용히 건너뛰지 않고 소스 실패로 드러나야 한다."""
    from src import robots
    from src.collectors import board
    from src.collectors.base import CollectError, RunContext
    from src.dedup import SeenStore

    robots.reset_cache()
    monkeypatch.setattr(
        robots, "fetch_bytes",
        lambda url, **kw: (b"User-agent: *\nDisallow: /not/\n", "utf-8"),
    )
    called = []
    monkeypatch.setattr(board, "fetch_bytes", lambda *a, **kw: called.append(a) or (b"", None))

    store = SeenStore(":memory:")
    try:
        with pytest.raises(CollectError, match="robots.txt"):
            board.collect(board_source(), RunContext(seen_store=store))
        assert called == []  # 페이지 요청 자체가 나가지 않아야 함
    finally:
        store.close()
        robots.reset_cache()


def test_collect_proceeds_when_robots_allows(monkeypatch):
    from src import robots
    from src.collectors import board
    from src.collectors.base import RunContext
    from src.dedup import SeenStore

    robots.reset_cache()
    monkeypatch.setattr(
        robots, "fetch_bytes", lambda url, **kw: (b"User-agent: *\nDisallow: /admin/\n", "utf-8")
    )
    monkeypatch.setattr(
        board, "fetch_bytes", lambda *a, **kw: (BOARD_HTML.encode("utf-8"), "utf-8")
    )

    store = SeenStore(":memory:")
    try:
        items = board.collect(board_source(), RunContext(seen_store=store))
        assert len(items) == 2
    finally:
        store.close()
        robots.reset_cache()


# ── 제목 앞 [태그]에서 원 기관 뽑기 (협회 게시판 전재글) ──────────────────────

from src.collectors.board import split_title_tags  # noqa: E402

KIRA_TAGS = {"ignore": ["건축계소식", "건축세미나", "공지"], "as_org": True, "strip": True}


def test_second_tag_is_the_originating_agency():
    title, org = split_title_tags(
        "[건축계소식] [창원시]창원시 도시계획위원회 위원 공개모집 안내", KIRA_TAGS)
    assert org == "창원시"
    assert title == "창원시 도시계획위원회 위원 공개모집 안내"


def test_category_only_title_has_no_agency():
    title, org = split_title_tags("[건축세미나] 물류시설 피난설계 세미나", KIRA_TAGS)
    assert org is None
    assert title == "물류시설 피난설계 세미나"


def test_untagged_title_is_untouched():
    assert split_title_tags("당진성모병원 신축공사 건축설계공모", KIRA_TAGS) == (
        "당진성모병원 신축공사 건축설계공모", None)


def test_tags_are_not_stripped_when_asked_not_to():
    spec = dict(KIRA_TAGS, strip=False)
    title, org = split_title_tags("[건축계소식] [안양시]제안서 평가위원 공개모집", spec)
    assert org == "안양시"
    assert title == "[건축계소식] [안양시]제안서 평가위원 공개모집"


def test_title_that_is_only_tags_keeps_its_text():
    """태그만 있는 제목에서 태그를 다 떼면 빈 문자열이 된다 — 그 글은 사라진다."""
    title, org = split_title_tags("[건축계소식][창원시]", KIRA_TAGS)
    assert title == "[건축계소식][창원시]"
    assert org == "창원시"


def test_list_page_prefers_the_writer_column_over_the_title_tag():
    """글쓴이 칸이 있는 게시판에서는 그쪽이 더 정확하다."""
    from src.config import SourceConfig
    html = """<table><tr>
      <td class="board-title"><a href="?num=1">[건축계소식] [창원시]위원 공개모집</a></td>
      <td class="text-center">재정경제부</td></tr></table>"""
    source = SourceConfig(
        id="t", name="t", type="board", category="committee",
        options={
            "row_selector": "table tr:has(a)",
            "url_base": "https://ex.com/board/",
            "fields": {"title": {"selector": "a", "attr": "text"},
                       "link": {"selector": "a", "attr": "href"},
                       "org": {"selector": "td.text-center", "attr": "text"}},
            "title_tags": KIRA_TAGS,
        },
    )
    items = parse_list_page(html, source, "https://ex.com/board/")
    assert items[0].extra["org"] == "재정경제부"
    assert items[0].title == "위원 공개모집"


def test_via_curl_is_used_for_servers_that_drop_httpx(monkeypatch):
    """'Server disconnected' 내는 구형 서버용 우회 경로 — GET에만 적용된다."""
    from src.collectors import board as board_mod
    from src.config import SourceConfig

    calls = {"curl": 0, "httpx": 0}
    monkeypatch.setattr(board_mod, "fetch_via_curl",
                        lambda url, **kw: (calls.__setitem__("curl", calls["curl"] + 1),
                                           b"<table><tr><td><a href='/v?id=7'>\xea\xb3\xb5\xea\xb3\xa0</a></td></tr></table>")[1])
    monkeypatch.setattr(board_mod, "fetch_bytes",
                        lambda *a, **k: (calls.__setitem__("httpx", calls["httpx"] + 1), (b"", None))[1])
    monkeypatch.setattr(board_mod, "can_fetch", lambda *a, **k: True)
    monkeypatch.setattr(board_mod, "wait_for_host", lambda *a, **k: None)

    source = SourceConfig(
        id="old", name="구형", type="board", category="committee",
        options={"list_url": "https://ex.com/l", "via_curl": True,
                 "row_selector": "tr:has(a)", "url_base": "https://ex.com/",
                 "fields": {"title": {"selector": "a", "attr": "text"},
                            "link": {"selector": "a", "attr": "href"}}},
    )
    items = board_mod.collect(source, None)
    assert calls == {"curl": 1, "httpx": 0}
    assert len(items) == 1


def test_via_curl_is_ignored_for_post_boards(monkeypatch):
    """curl 경로는 폼 전송을 하지 않는다 — POST 게시판에 쓰면 조용히 빈 목록이 된다."""
    from src.collectors import board as board_mod
    from src.config import SourceConfig

    calls = {"curl": 0, "httpx": 0}
    monkeypatch.setattr(board_mod, "fetch_via_curl",
                        lambda url, **kw: (calls.__setitem__("curl", calls["curl"] + 1), b"")[1])
    monkeypatch.setattr(board_mod, "fetch_bytes",
                        lambda *a, **k: (calls.__setitem__("httpx", calls["httpx"] + 1),
                                         (b"<table></table>", None))[1])
    monkeypatch.setattr(board_mod, "can_fetch", lambda *a, **k: True)
    monkeypatch.setattr(board_mod, "wait_for_host", lambda *a, **k: None)

    source = SourceConfig(
        id="p", name="폼", type="board", category="committee",
        options={"list_url": "https://ex.com/l", "via_curl": True, "method": "POST",
                 "form_data": {"pageIndex": "{page}"}, "row_selector": "tr:has(a)",
                 "fields": {"title": {"selector": "a", "attr": "text"},
                            "link": {"selector": "a", "attr": "href"}}},
    )
    board_mod.collect(source, None)
    assert calls == {"curl": 0, "httpx": 1}
