from datetime import datetime, timedelta, timezone

from src.dedup import SeenStore
from src.models import Item


def make_item(key: str = "k1", **kw) -> Item:
    defaults = dict(
        source_id="src1", category="news", title="제목", url="https://ex.com/1",
        natural_key=key, key_prefix="rss:src1",
    )
    defaults.update(kw)
    return Item(**defaults)


def test_mark_and_is_seen(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    item = make_item()
    assert not store.is_seen(item.dedup_key)
    store.mark_seen(item)
    assert store.is_seen(item.dedup_key)
    store.close()


def test_mark_seen_idempotent(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    item = make_item()
    store.mark_seen(item)
    store.mark_posted(item.dedup_key, 42)
    store.mark_seen(item)  # INSERT OR IGNORE — posted 상태 유지
    rows = store.recent(days=1, statuses=("posted",))
    assert len(rows) == 1
    assert rows[0]["tg_message_id"] == 42
    store.close()


def test_posted_between(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    item = make_item()
    store.mark_seen(item)
    store.mark_posted(item.dedup_key, 7)
    now = datetime.now(timezone.utc)
    rows = store.posted_between(now - timedelta(hours=1), now + timedelta(hours=1))
    assert len(rows) == 1
    assert rows[0]["title"] == "제목"
    assert store.posted_between(now + timedelta(hours=2), now + timedelta(hours=3)) == []
    store.close()


def test_meta_roundtrip(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    assert store.get_meta("k") is None
    store.set_meta("k", "v1")
    store.set_meta("k", "v2")
    assert store.get_meta("k") == "v2"
    store.close()


def test_source_has_rows(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    assert not store.source_has_rows("src1")
    store.mark_seen(make_item())
    assert store.source_has_rows("src1")
    assert not store.source_has_rows("other")
    store.close()


def test_dedup_key_fallback_url_hash():
    item = make_item(natural_key=None, key_prefix="")
    assert item.dedup_key.startswith("url:")
    item2 = make_item(natural_key=None, key_prefix="", url="https://ex.com/1")
    assert item.dedup_key == item2.dedup_key


def test_pending_roundtrip(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    item = make_item(key="p1")
    store.mark_seen(item, status="pending")
    rows = store.pending()
    assert len(rows) == 1 and rows[0]["id"] == item.dedup_key
    store.mark_posted(item.dedup_key, 9)
    assert store.pending() == []
    store.close()


def test_mark_status_removes_from_pending(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    item = make_item(key="f1")
    store.mark_seen(item, status="pending")
    store.mark_status(item.dedup_key, "failed")
    assert store.pending() == []          # 재시도 큐에서 빠짐
    assert store.is_seen(item.dedup_key)  # 중복 재수집은 여전히 차단
    store.close()


def test_restored_row_keeps_its_stored_key(tmp_path):
    """복원 Item의 키가 저장된 id와 달라지면 mark_posted가 0행을 갱신하고,
    그 항목은 영원히 pending으로 남아 매일 재전송된다."""
    from src.main import _item_from_row

    store = SeenStore(tmp_path / "t.db")
    it = Item(source_id="aik_news", category="committee", title="창원시 도시계획위원회 위원 공개모집",
              url="https://ex.com/8630", natural_key="8630", key_prefix="board:aik_news")
    store.mark_seen(it, status="pending")

    restored = _item_from_row(store.pending()[0])
    assert restored.dedup_key == it.dedup_key

    store.mark_posted(restored.dedup_key, 42)
    assert store.pending() == []
    store.close()
