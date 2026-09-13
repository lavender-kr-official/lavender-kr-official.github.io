from src.collectors.eminwon import parse_list_html
from src.config import SourceConfig

HTML = """
<table>
<tr><td><a href="#" onclick="searchDetail(123456); return false;">건설기술심의위원회 위원 공개모집</a></td></tr>
<tr><td><a href="#" onclick="searchDetail(123457)">일반 고시공고</a></td></tr>
<tr><td><a href="#" onclick="searchDetail(123456)">중복 링크</a></td></tr>
<tr><td><a href="/other">무관한 링크</a></td></tr>
</table>
"""


def src() -> SourceConfig:
    return SourceConfig(id="emw1", name="기장군", type="eminwon", category="committee",
                        options={"host": "eminwon.gijang.go.kr"})


def test_parse_list_html():
    items = parse_list_html(HTML, src(), "eminwon.gijang.go.kr")
    assert len(items) == 2  # 중복·무관 링크 제외
    first = items[0]
    assert first.natural_key == "123456"
    assert first.dedup_key == "emw:eminwon.gijang.go.kr:123456"
    assert "eminwon.gijang.go.kr" in first.url
    assert "123456" in first.url


# ── robots.txt 준수 (지자체 수백 곳으로 늘어날 수집기다) ──────────────────────

import pytest  # noqa: E402

from src import robots  # noqa: E402
from src.collectors import eminwon  # noqa: E402
from src.collectors.base import CollectError  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_robots():
    robots.reset_cache()
    yield
    robots.reset_cache()


def _ctx():
    class Ctx:
        slot = "morning"
        secrets: dict = {}
    return Ctx()


def test_disallowed_host_raises_instead_of_silently_skipping(monkeypatch):
    """조용히 0건을 돌려주면 게시판이 비어 있는 날과 구분되지 않는다."""
    monkeypatch.setattr(eminwon, "can_fetch", lambda url, **kw: False)
    monkeypatch.setattr(eminwon, "fetch_bytes",
                        lambda *a, **k: pytest.fail("금지된 호스트에 요청하면 안 됨"))
    with pytest.raises(CollectError, match="robots.txt"):
        eminwon.collect(src(), _ctx())


def test_allowed_host_waits_for_crawl_delay(monkeypatch):
    waited = []
    monkeypatch.setattr(eminwon, "can_fetch", lambda url, **kw: True)
    monkeypatch.setattr(eminwon, "wait_for_host", lambda url, **kw: waited.append(url))
    monkeypatch.setattr(eminwon, "fetch_bytes", lambda *a, **k: (HTML.encode(), "utf-8"))
    items = eminwon.collect(src(), _ctx())
    assert waited and waited[0].startswith("https://eminwon.gijang.go.kr")
    assert len(items) == 2  # 중복 링크는 합쳐지고 무관한 링크는 빠진다


def test_opt_out_is_explicit(monkeypatch):
    """respect_robots: false는 명시적으로만. 그때는 robots를 아예 조회하지 않는다."""
    monkeypatch.setattr(eminwon, "can_fetch", lambda *a, **k: pytest.fail("조회하면 안 됨"))
    monkeypatch.setattr(eminwon, "fetch_bytes", lambda *a, **k: (HTML.encode(), "utf-8"))
    source = src()
    source.options["respect_robots"] = False
    assert len(eminwon.collect(source, _ctx())) == 2
