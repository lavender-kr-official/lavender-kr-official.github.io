"""자체 사이트(GitHub Pages) 빌드: data.json + 일간 브리핑 글 + sitemap + RSS.

site/index.html(대시보드 템플릿)은 정적 파일로 저장소에 존재하고,
이 모듈은 데이터(data.json)와 posts/·sitemap.xml·feed.xml만 재생성한다.
"""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import SiteConfig
from .deadline import extract_deadline, parse_deadline_str
from .intent import is_postable
from .dedup import SeenStore
from .llm import summarize_items
from .models import CATEGORY_META

KST = timezone(timedelta(hours=9))
SITE_DIR = Path("site")
POSTS_DIR = SITE_DIR / "posts"

ACTIVE_STATUSES = ("posted", "seen", "skipped", "pending")  # "failed"는 제외
NEWS_LIKE = ("news", "gov", "association")
COMMITTEE_UNKNOWN_DEADLINE_DAYS = 30
BID_UNKNOWN_DEADLINE_DAYS = 14


def _now_kst() -> datetime:
    return datetime.now(KST)


def _item_deadline(item: dict) -> date | None:
    raw = (item.get("extra") or {}).get("deadline")
    if raw:
        parsed = parse_deadline_str(str(raw))
        if parsed:
            return parsed
    return extract_deadline(item.get("title") or "", today=_now_kst().date())


def _first_seen_date(item: dict) -> date | None:
    raw = item.get("first_seen_at") or ""
    try:
        return datetime.fromisoformat(raw).astimezone(KST).date()
    except ValueError:
        return None


def _row(item: dict, deadline: date | None) -> dict:
    extra = item.get("extra") or {}
    today = _now_kst().date()
    return {
        "title": item.get("title"),
        "url": item.get("url"),
        "org": extra.get("org") or item.get("author") or "",
        "source": item.get("author") or item.get("source_id") or "",
        "deadline": deadline.isoformat() if deadline else None,
        "d_day": (deadline - today).days if deadline else None,
        "date": (_first_seen_date(item) or today).isoformat(),
        "thumbnail": extra.get("thumbnail"),
        "channel": extra.get("channel"),
        "amount": extra.get("amount"),
    }


def build_data(store: SeenStore) -> dict:
    items = store.recent(days=90, statuses=ACTIVE_STATUSES)
    today = _now_kst().date()
    committees: list[dict] = []
    bids: list[dict] = []
    news: list[dict] = []
    videos: list[dict] = []

    for item in items:
        cat = item.get("category") or "news"
        # 명단·위촉 결과는 사이트에도 싣지 않는다. 텔레그램은 수집·표시 두 곳에서
        # 막는데 사이트만 뚫려 있었다 — 공개 색인되는 쪽이라 더 위험한 누출이다.
        # 지원할 수 없는 정보이고 사람 이름이 들어 있다.
        if not is_postable(item.get("title") or ""):
            continue
        if cat == "committee":
            deadline = _item_deadline(item)
            seen = _first_seen_date(item) or today
            if deadline is not None:
                if deadline >= today:
                    committees.append(_row(item, deadline))
            elif (today - seen).days <= COMMITTEE_UNKNOWN_DEADLINE_DAYS:
                committees.append(_row(item, None))
        elif cat == "bid":
            deadline = _item_deadline(item)
            seen = _first_seen_date(item) or today
            if deadline is not None:
                if deadline >= today:
                    bids.append(_row(item, deadline))
            elif (today - seen).days <= BID_UNKNOWN_DEADLINE_DAYS:
                bids.append(_row(item, None))
        elif cat == "youtube":
            videos.append(_row(item, None))
        elif cat in NEWS_LIKE:
            news.append(_row(item, None))

    # 마감 임박순 (마감 미상은 뒤로), 뉴스·영상은 최신순
    committees.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or "", r["date"]))
    bids.sort(key=lambda r: (r["deadline"] is None, r["deadline"] or "", r["date"]))
    news.sort(key=lambda r: r["date"], reverse=True)
    videos.sort(key=lambda r: r["date"], reverse=True)
    return {
        "generated_at": _now_kst().isoformat(),
        "committees": committees,
        "bids": bids,
        "news": news[:500],
        "videos": videos[:120],
    }


def _post_html(day: date, items: list[dict], site: SiteConfig, summary: str | None) -> str:
    date_str = day.strftime("%Y년 %m월 %d일")
    title = f"{date_str} 건설업계 브리핑"
    by_cat: dict[str, list[dict]] = {}
    for it in items:
        by_cat.setdefault(it.get("category") or "news", []).append(it)

    sections: list[str] = []
    for cat in ("news", "gov", "bid", "committee", "association", "youtube"):
        cat_items = by_cat.get(cat)
        if not cat_items:
            continue
        meta = CATEGORY_META.get(cat, {"emoji": "", "label": cat})
        rows = "\n".join(
            f'<li><a href="{html.escape(it.get("url") or "#")}" target="_blank" rel="noopener">'
            f'{html.escape(it.get("title") or "")}</a>'
            f'<span class="src"> — {html.escape((it.get("extra") or {}).get("org") or it.get("author") or "")}</span></li>'
            for it in cat_items
        )
        sections.append(
            f'<section><h2>{meta["emoji"]} {meta["label"]}</h2><ul>{rows}</ul></section>'
        )

    tg_button = (
        f'<p class="tg"><a class="btn" href="{html.escape(site.telegram_channel_url)}">'
        "📱 텔레그램 채널에서 실시간으로 받아보기</a></p>"
        if site.telegram_channel_url else ""
    )
    summary_html = f"<p class='summary'>{html.escape(summary)}</p>" if summary else ""
    desc = summary or f"{date_str} 건설·토목·건축 뉴스, 입찰공고, 위원회 모집 브리핑"
    canonical = f"{site.base_url}/posts/{day.isoformat()}.html" if site.base_url else ""
    canonical_tag = f'<link rel="canonical" href="{html.escape(canonical)}">' if canonical else ""
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} | {html.escape(site.title)}</title>
<meta name="description" content="{html.escape(desc[:150])}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(desc[:150])}">
<meta property="og:type" content="article">
{canonical_tag}
<link rel="stylesheet" href="../style.css">
</head>
<body>
<header><a href="../index.html">← {html.escape(site.title)}</a></header>
<main class="post">
<h1>{html.escape(title)}</h1>
<p class="date">{day.isoformat()}</p>
{summary_html}
{"".join(sections)}
{tg_button}
<footer><p>본 글은 공개된 뉴스·공고의 제목과 링크를 모아 정리한 것입니다. 본문은 수집하지 않으며 각 항목의 자세한 내용은 출처 링크를 확인해 주세요.</p><p><a href="../about.html">안내 및 문의 · 게시 중단 요청</a></p></footer>
</main>
</body>
</html>
"""


def build_daily_post(store: SeenStore, site: SiteConfig, day: date | None = None,
                     *, use_llm: bool = True) -> Path | None:
    day = day or _now_kst().date()
    start = datetime(day.year, day.month, day.day, tzinfo=KST)
    end = start + timedelta(days=1)
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    items = store.posted_between(start_utc, end_utc)
    if not items:  # 텔레그램 미설정 등으로 게시분이 없으면 당일 수집분으로 브리핑 구성
        items = store.collected_between(start_utc, end_utc, limit=40)
    if not items:
        return None
    titles = [f"[{it.get('category')}] {it.get('title')}" for it in items]
    summary = summarize_items(titles) if use_llm else None
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTS_DIR / f"{day.isoformat()}.html"
    path.write_text(_post_html(day, items, site, summary), encoding="utf-8")
    return path


def _existing_posts() -> list[str]:
    if not POSTS_DIR.exists():
        return []
    return sorted(
        (p.stem for p in POSTS_DIR.glob("????-??-??.html")), reverse=True
    )


def build_posts_index(site: SiteConfig) -> None:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    links = "\n".join(
        f'<li><a href="{d}.html">{d} 건설업계 브리핑</a></li>' for d in _existing_posts()
    )
    POSTS_DIR.joinpath("index.html").write_text(f"""<!DOCTYPE html>
<html lang="ko">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>브리핑 아카이브 | {html.escape(site.title)}</title>
<link rel="stylesheet" href="../style.css"></head>
<body>
<header><a href="../index.html">← {html.escape(site.title)}</a></header>
<main class="post"><h1>일간 브리핑 아카이브</h1><ul>{links}</ul></main>
</body></html>
""", encoding="utf-8")


def build_sitemap(site: SiteConfig) -> None:
    if not site.base_url:
        return
    urls = [f"{site.base_url}/", f"{site.base_url}/about.html", f"{site.base_url}/posts/"]
    urls += [f"{site.base_url}/posts/{d}.html" for d in _existing_posts()]
    body = "\n".join(
        f"  <url><loc>{html.escape(u)}</loc></url>" for u in urls
    )
    SITE_DIR.joinpath("sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}\n</urlset>\n",
        encoding="utf-8",
    )


def build_feed(site: SiteConfig) -> None:
    if not site.base_url:
        return
    entries = []
    for d in _existing_posts()[:30]:
        url = f"{site.base_url}/posts/{d}.html"
        entries.append(
            "  <item>\n"
            f"    <title>{d} 건설업계 브리핑</title>\n"
            f"    <link>{html.escape(url)}</link>\n"
            f"    <guid>{html.escape(url)}</guid>\n"
            "  </item>"
        )
    SITE_DIR.joinpath("feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>\n'
        f"  <title>{html.escape(site.title)}</title>\n"
        f"  <link>{html.escape(site.base_url)}</link>\n"
        f"  <description>{html.escape(site.description)}</description>\n"
        + "\n".join(entries)
        + "\n</channel></rss>\n",
        encoding="utf-8",
    )


def build_site(store: SeenStore, site: SiteConfig, *, use_llm: bool = True,
               with_daily_post: bool = True) -> None:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    data = build_data(store)
    data["site"] = {
        "title": site.title,
        "description": site.description,
        "telegram": site.telegram_channel_url,
    }
    SITE_DIR.joinpath("data.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    if with_daily_post:
        build_daily_post(store, site, use_llm=use_llm)
    build_posts_index(site)
    build_sitemap(site)
    build_feed(site)
