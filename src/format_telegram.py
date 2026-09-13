"""텔레그램 메시지 포맷팅 — 하루치를 한 통으로 묶는 일간 다이제스트."""
from __future__ import annotations

import re

from .models import Item
from .intent import COMPETITION, RECRUIT, ROSTER, classify_intent
from .topics import classify, topic_order

TG_LIMIT = 4096  # 텔레그램 메시지 한도 (참고용, 실제 경계는 SAFE_LIMIT)
SAFE_LIMIT = 4000  # 여유분


def escape_html(s: str) -> str:
    """텔레그램 HTML 규칙 + href 속성 안전을 위한 따옴표 이스케이프."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def split_blocks(blocks: list[str], limit: int = SAFE_LIMIT) -> list[str]:
    """블록(줄) 단위로만 분할 — 태그 중간에서 절대 자르지 않음."""
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        add = len(block) + (1 if current else 0)
        if current and size + add > limit:
            messages.append("\n".join(current))
            current, size = [], 0
            add = len(block)
        current.append(block)
        size += add
    if current:
        messages.append("\n".join(current))
    return messages


# ── 일간 다이제스트 ──────────────────────────────────────────────────────────
# 하루 한 번, 수집분 전체를 한 통(필요 시 여러 통)으로 묶어 보낸다.
# 건별 메시지는 하루 수십 통이 되어 검토가 불가능했다.

WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")

# 한 분야가 지면을 독점하지 않게 제한
TOPIC_CAP = 6
# 한 매체가 뉴스 지면을 독점하지 않게 제한
PER_SOURCE_CAP = 8
# 다이제스트 제목 상한 — 한 줄에 들어오게. 전문은 사이트에서 본다
DIGEST_TITLE_MAX = 90
# 다이제스트에 실을 입찰 추정가 하한. 실측상 전체의 9%만 이 선을 넘는다.
# 소액 용역까지 실으면 지면을 다 먹는다 — 전체 목록은 사이트에 있다.
BID_MIN_AMOUNT = 1_000_000_000
BID_MIN_LABEL = "10억"

# 섹션 순서 = 독자 가치 순. 위원 모집은 다른 데서 찾기 어려운 정보라 맨 앞이고
# 상한을 두지 않는다 — 실측에서 상한이 세미나를 살리고 위원 모집을 버렸다.
DIGEST_SECTIONS = (
    ("recruit", "👥 위원 모집", None),
    ("bid", f"📋 입찰공고 (추정가 {BID_MIN_LABEL} 이상)", 12),
    ("competition", "🏗 설계공모", 8),
    ("gov", "🏢 정책", 10),
    ("association", "🏛 협회 소식", 10),
    ("news", "📰 뉴스", 30),
    ("youtube", "🎬 영상", 5),
)


def section_of(item: Item) -> str:
    """항목이 들어갈 다이제스트 구획. committee만 제목 의도로 다시 가른다.

    협회 게시판 하나에 위원 모집·설계공모·세미나가 뒤섞여 들어온다. 한 구획에
    몰아두면 정작 지원할 수 있는 공고가 세미나 안내에 묻힌다.
    """
    category = item.category or "news"
    if category != "committee":
        return category
    intent = classify_intent(item.title or "")
    if intent == RECRUIT:
        return "recruit"
    if intent == COMPETITION:
        return "competition"
    return "association"  # 세미나·행사·기타 협회 소식


_AMOUNT_DIGITS_RE = re.compile(r"[\d,]+")


def parse_won(raw: str) -> int | None:
    """'1,754,181,818원' → 1754181818. 숫자를 못 찾으면 None."""
    m = _AMOUNT_DIGITS_RE.search(raw or "")
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def compact_amount(raw: str) -> str:
    """'1,754,181,818원' → '17.5억'. 다이제스트에서 자릿수가 줄을 잡아먹는다."""
    won = parse_won(raw)
    if won is None:
        return raw
    if won >= 100_000_000:
        return f"{won / 100_000_000:.1f}".rstrip("0").rstrip(".") + "억"
    if won >= 10_000:
        return f"{won // 10_000:,}만"
    return f"{won:,}원"


def compact_deadline(raw: str) -> str:
    """'2026-09-21 18:00' → '9/21 18:00'. 연도는 거의 항상 올해다."""
    m = re.match(r"(\d{4})[-.](\d{1,2})[-.](\d{1,2})(?:\s+(\d{1,2}:\d{2}))?", str(raw or ""))
    if not m:
        return str(raw)
    _, month, day, time = m.groups()
    stamp = f"{int(month)}/{int(day)}"
    return f"{stamp} {time}" if time else stamp


# ── 종목 시세 ───────────────────────────────────────────────────────────────
# 다이제스트 맨 위, 공고·뉴스보다 먼저. 증권사 채널이 시황으로 문을 여는 이유와
# 같다 — 매일 같은 자리에 있어야 훑고 지나갈 수 있다.

KR_QUOTE_CAP = 6
US_QUOTE_CAP = 5


def _arrow(pct: float | None) -> str:
    if pct is None:
        return "―"
    if pct > 0:
        return f"▲{pct:.2f}%"
    if pct < 0:
        return f"▼{abs(pct):.2f}%"
    return "―0.00%"


def _price(quote) -> str:
    if quote.currency == "USD":
        return f"${quote.close:,.2f}"
    return f"{quote.close:,.0f}"


def _quote_group(label: str, quotes: list, cap: int) -> list[str]:
    if not quotes:
        return []
    days = [q.as_of for q in quotes if q.as_of]
    stamp = ""
    if days:
        newest = max(days)
        stamp = f" <i>{newest.month}/{newest.day} 종가</i>"
    lines = [f"<b>{escape_html(label)}</b>{stamp}"]
    for q in quotes[:cap]:
        lines.append(f"{escape_html(q.name)} {_price(q)} {_arrow(q.change_pct)}")
    return lines


def format_quotes(kr: list, us: list) -> list[str]:
    """시세 블록 (없으면 빈 목록). 국내·미국을 각각 한 묶음으로."""
    blocks: list[str] = []
    for label, quotes, cap in (("📈 건설주", kr, KR_QUOTE_CAP),
                               ("📈 미국 건설·인프라", us, US_QUOTE_CAP)):
        group = _quote_group(label, quotes, cap)
        if group:
            blocks.append("")
            blocks.extend(group)
    return blocks


def _digest_meta_line(item: Item) -> str:
    """항목 아래 붙는 부가 정보 한 줄. 없으면 빈 문자열."""
    bits: list[str] = []
    org = item.extra.get("org")
    if org:
        bits.append(escape_html(str(org)))
    deadline = item.extra.get("deadline")
    if deadline:
        bits.append(f"마감 {escape_html(compact_deadline(deadline))}")
    amount = item.extra.get("amount")
    if amount:
        bits.append(escape_html(compact_amount(str(amount))))
    return " · ".join(bits)


def _digest_entry(item: Item, *, show_source: bool = False) -> list[str]:
    title = escape_html(item.title.strip())
    if len(title) > DIGEST_TITLE_MAX:
        title = title[:DIGEST_TITLE_MAX - 1].rstrip() + "…"
    href = escape_html(item.url)
    anchor = f'▶️ <a href="{href}">{title}</a>'
    if len(anchor) > SAFE_LIMIT:
        # URL 자체가 한도를 넘는 병리적 케이스 — 링크를 버리고 제목만 남긴다.
        # 한 블록이 한도를 넘으면 split_blocks도 쪼갤 수 없어 전송이 400으로 죽는다.
        anchor = f"▶️ {title}"
    lines = [anchor]
    meta = _digest_meta_line(item)
    if not meta and show_source and item.author:
        # 분야로 묶으면 매체명이 헤더에서 사라지므로 항목에 붙여 출처를 남긴다
        meta = escape_html(str(item.author))
    if meta:
        lines.append(meta)
    return lines


def _select_bids(items: list[Item]) -> list[Item]:
    """추정가 하한 이상만, 금액 큰 순으로. 금액 미상은 판단할 수 없어 남긴다."""
    kept = []
    for it in items:
        won = parse_won(str(it.extra.get("amount") or ""))
        if won is None or won >= BID_MIN_AMOUNT:
            kept.append((won or 0, it))
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [it for _, it in kept]


def _balance_by_source(items: list[Item], cap: int) -> tuple[list[Item], int]:
    """매체별 상한을 적용하고 매체를 번갈아 배치한다. (남은 목록, 잘린 건수)

    단순히 상한만 걸면 뒤이어 적용되는 분야 상한이 균형을 다시 무너뜨린다.
    발행량 많은 매체 기사가 앞쪽을 채우고 소수 매체는 잘려 나간다.
    라운드로빈으로 섞어야 어느 단계에서 잘리든 매체가 고루 남는다.
    """
    buckets: dict[str, list[Item]] = {}
    for it in items:
        buckets.setdefault(it.author or it.source_id, []).append(it)

    dropped = sum(max(0, len(v) - cap) for v in buckets.values())
    queues = [v[:cap] for v in buckets.values()]
    kept: list[Item] = []
    for i in range(cap):
        for q in queues:
            if i < len(q):
                kept.append(q[i])
    return kept, dropped


def _group_by_topic(items: list[Item]) -> list[tuple[str, list[Item]]]:
    """분야별로 묶되 topics.py가 정한 순서를 따른다. 빈 분야는 건너뛴다."""
    grouped: dict[str, list[Item]] = {}
    for it in items:
        grouped.setdefault(classify(it.title or ""), []).append(it)
    return [(name, grouped[name]) for name in topic_order() if name in grouped]


def format_daily_digest(items: list[Item], day, site_url: str = "",
                        title: str = "라벤더", quotes: tuple[list, list] | None = None) -> list[str]:
    """수집분을 섹션별로 묶은 일간 다이제스트. 4096자 경계에서 여러 통으로 나뉜다."""
    date_str = f"{day.year}년 {day.month}월 {day.day}일"
    header = f"<b>[{escape_html(title)}] {date_str} ({WEEKDAYS[day.weekday()]})</b>"
    blocks: list[str] = [header]
    if quotes:
        blocks.extend(format_quotes(*quotes))
    total = 0

    # 명단·위촉 결과는 여기까지 오지 않아야 하지만, 예전에 수집된 행이
    # 미리보기로 흘러들 수 있어 표시 단계에서도 한 번 더 막는다.
    items = [it for it in items if classify_intent(it.title or "") != ROSTER]
    for category, label, cap in DIGEST_SECTIONS:
        bucket = [it for it in items if section_of(it) == category]
        if category == "bid":
            bucket = _select_bids(bucket)
        if not bucket:
            continue
        shown, omitted = bucket, 0
        if cap is not None and len(bucket) > cap:
            shown, omitted = bucket[:cap], len(bucket) - cap

        blocks.append("")
        blocks.append(f"<b>{label}</b>")

        if category == "news":
            # 분야로 묶기 전에 매체 균형부터. 발행량이 많은 한 매체가 지면을
            # 독점하면 분야 그룹을 나눠도 결국 그 매체 기사만 보인다.
            shown, dropped = _balance_by_source(shown, PER_SOURCE_CAP)
            omitted += dropped
            # 분야별 그룹 헤더 — 읽는 사람이 관심 구간만 훑을 수 있게
            for topic, group in _group_by_topic(shown):
                blocks.append("")
                blocks.append(f"&lt;{escape_html(topic)}&gt;")
                for it in group[:TOPIC_CAP]:
                    blocks.extend(_digest_entry(it, show_source=True))
                    total += 1
                if len(group) > TOPIC_CAP:
                    omitted += len(group) - TOPIC_CAP
        else:
            for it in shown:
                blocks.extend(_digest_entry(it))
                total += 1

        if omitted:
            blocks.append(f"…외 {omitted}건")

    if total == 0:
        return []

    blocks.append("")
    if site_url:
        blocks.append(f'전체 공고·아카이브 → <a href="{escape_html(site_url)}">{escape_html(site_url)}</a>')
    return split_blocks(blocks)
