"""파이프라인 CLI 진입점.

  python -m src.main collect [--slot morning|noon|evening] [--sources a,b] [--dry-run] [--no-post]
  python -m src.main verify-sources
  python -m src.main smoke-g2b
  python -m src.main blog-draft [--date YYYY-MM-DD]
  python -m src.main build-site
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

from .blog_draft import build_draft, write_draft
from .collectors import COLLECTORS, CollectError, RunContext
from .collectors import narajangteo
from .config import AppConfig, apply_keyword_filters, load_config
from .dedup import SeenStore
from .format_telegram import format_daily_digest
from .httpio import decode_body, fetch_bytes, redact_secrets
from .models import Item
from .report import SourceResult, render_report, update_health, verify_sources, write_github_summary
from .site_build import build_site
from .stocks import fetch_quotes
from .telegram_client import TelegramClient, TelegramError

log = logging.getLogger("pipeline")
KST = timezone(timedelta(hours=9))
BOOTSTRAP_POST_LIMIT = 3
# 일간 산출물(브리핑 글·블로그 초안·LLM 요약)을 만드는 슬롯.
# cron이 아침 1회뿐이라 morning이 그날의 유일한 실행이다.
DAILY_SLOT = "morning"


def current_slot(now: datetime | None = None) -> str:
    """cron 지연에 강건하게, 실행 시각의 KST 기준으로 슬롯 결정."""
    hour = (now or datetime.now(KST)).astimezone(KST).hour
    if hour < 10:
        return "morning"
    if hour < 15:
        return "noon"
    return "evening"


def _make_ctx(cfg: AppConfig, slot: str) -> RunContext:
    return RunContext(
        seen_store=SeenStore(),
        slot=slot,
        secrets=cfg.secrets,
        site=cfg.site,
    )


def _telegram(cfg: AppConfig) -> tuple[TelegramClient | None, str]:
    token = cfg.secrets.get("TELEGRAM_BOT_TOKEN")
    chat_id = cfg.secrets.get("TELEGRAM_STAGING_CHAT_ID") or ""
    if not token or not chat_id:
        return None, chat_id
    return TelegramClient(token), chat_id


def _notify(cfg: AppConfig, tg: TelegramClient | None, text: str) -> None:
    if tg is None:
        log.warning("알림 전송 생략(텔레그램 미설정): %s", text)
        return
    admin = cfg.secrets.get("TELEGRAM_ADMIN_CHAT_ID")
    chat_id = admin or cfg.secrets.get("TELEGRAM_STAGING_CHAT_ID") or ""
    if chat_id:
        tg.notify_error(chat_id, text)


def _item_from_row(row: dict) -> Item:
    """pending 재시도용: seen 테이블 row를 포맷팅 가능한 Item으로 복원."""
    return Item(
        source_id=row.get("source_id") or "",
        category=row.get("category") or "news",
        title=row.get("title") or "",
        url=row.get("url") or "",
        author=row.get("author"),
        extra=row.get("extra") or {},
    )


def run_collect(args: argparse.Namespace) -> int:
    cfg = load_config()
    slot = args.slot or current_slot()
    ctx = _make_ctx(cfg, slot)
    ctx.advance_cursor = not args.dry_run  # dry-run은 증분 워터마크를 움직이지 않음
    store = ctx.seen_store
    tg, staging_chat = _telegram(cfg)
    posting = not (args.dry_run or args.no_post)
    notices: list[str] = []
    if posting and tg is None:
        # 설정 전 단계 — 실패로 죽이면 cron마다 실패 메일만 쌓인다.
        # 게시만 생략하고 수집·사이트 빌드는 정상 진행해 사이트가 먼저 채워지게 한다.
        posting = False
        notices.append(
            "텔레그램 시크릿(TELEGRAM_BOT_TOKEN, TELEGRAM_STAGING_CHAT_ID) 미등록 — "
            "이번 실행은 게시 없이 수집·사이트 빌드만 수행했습니다. docs/checklist.md 1번 참조."
        )
        log.warning(notices[-1])

    wanted = set(args.sources.split(",")) if args.sources else None
    sources = [
        s for s in cfg.sources
        if s.enabled and slot in s.slots and (wanted is None or s.id in wanted)
    ]
    log.info("slot=%s, 대상 소스 %d개", slot, len(sources))

    errors: list[str] = []
    health_results: list[SourceResult] = []
    posted_total = 0
    site_url = cfg.site.base_url
    # 이번 실행에서 다이제스트에 실을 아이템 (직전 실행에서 못 보낸 pending 포함)
    digest_items: list[Item] = []
    if posting and tg is not None:
        digest_items.extend(_item_from_row(r) for r in store.pending(limit=200))

    for source in sources:
        collector = COLLECTORS.get(source.type)
        if collector is None:
            errors.append(f"{source.id}: 알 수 없는 type {source.type}")
            continue
        try:
            items = collector(source, ctx)
        except CollectError as exc:
            # 예외 문자열에 요청 URL(=API 키·릴레이 주소)이 섞일 수 있어 항상 마스킹
            msg = redact_secrets(str(exc), cfg.secrets.values())[:300]
            log.warning("소스 실패 %s: %s", source.id, msg)
            errors.append(f"{source.id}: {msg}")
            health_results.append(SourceResult(source.id, source.name, False, error=msg))
            continue
        except Exception as exc:  # noqa: BLE001 — 개별 소스의 예상 못한 오류도 격리
            msg = redact_secrets(f"{type(exc).__name__}: {exc}", cfg.secrets.values())[:300]
            log.error("소스 예외 %s: %s", source.id, msg)
            errors.append(f"{source.id}: {msg}")
            health_results.append(SourceResult(source.id, source.name, False, error=msg))
            continue

        health_results.append(SourceResult(source.id, source.name, True, count=len(items)))
        fresh = [
            it for it in items
            if apply_keyword_filters(it.title, source.filters)
            and not store.is_seen(it.dedup_key)
        ]
        # 같은 run 안에서의 소스 내 중복 제거 (동일 키 두 번 등장 방어)
        unique: dict[str, Item] = {}
        for it in fresh:
            unique.setdefault(it.dedup_key, it)
        fresh = list(unique.values())
        if not fresh:
            continue
        fresh.sort(key=lambda it: it.published_at or it.fetched_at)

        bootstrap = not store.source_has_rows(source.id)
        cap = BOOTSTRAP_POST_LIMIT if bootstrap else source.max_new_per_run
        if cap <= 0:
            to_post, overflow = [], fresh
        else:
            to_post, overflow = fresh[-cap:], fresh[:-cap]
        if bootstrap:
            log.info("'%s' 첫 수집(bootstrap): %d건 중 최신 %d건만 게시", source.id, len(fresh), len(to_post))

        if args.dry_run:
            for it in fresh:
                log.info("[dry-run] %s | %s", it.dedup_key, it.title)
            continue

        # 수집분은 게시 성패와 무관하게 즉시 영속화 —
        # to_post는 'pending'(그날 다이제스트 대상), overflow는 사이트에만 노출
        for it in overflow:
            store.mark_seen(it, status="skipped" if not bootstrap else "seen")
        for it in to_post:
            store.mark_seen(it, status="pending" if posting else "seen")
            if posting:
                digest_items.append(it)

    # 수집이 끝난 뒤 하루치를 한 통(필요 시 여러 통)으로 묶어 보낸다.
    # 건별 전송은 하루 수십 통이 되어 검토가 불가능했다.
    if posting and tg is not None and digest_items:
        # 시세는 부가 정보 — 실패해도 공고·뉴스 배달을 막지 않는다
        try:
            quotes = fetch_quotes(cfg.stocks, cfg.secrets)
        except Exception as exc:  # noqa: BLE001
            log.warning("시세 조회 실패: %s", str(exc)[:200])
            quotes = ([], [])
        msgs = format_daily_digest(
            digest_items, datetime.now(KST).date(), site_url,
            title=cfg.site.title, quotes=quotes,
        )
        sent_all, last_mid = True, None
        for msg in msgs:
            try:
                last_mid = tg.send_message(staging_chat, msg, {"is_disabled": True})
            except TelegramError as exc:
                # 일부만 보내고 실패하면 전체를 pending으로 남겨 다음 실행에서 다시 만든다.
                # 이미 나간 메시지는 중복되지만, 누락보다 낫다.
                errors.append(f"다이제스트 게시 실패: {exc}")
                sent_all = False
                break
        if sent_all:
            for it in digest_items:
                store.mark_posted(it.dedup_key, last_mid)
            posted_total = len(digest_items)

    if not args.dry_run:
        store.prune()  # 오래된 미게시 row 정리 (state/seen.db 무한 증가 방지)
        alerts = update_health(health_results)
        for sid in alerts:
            _notify(cfg, tg, f"소스 '{sid}' 3회 연속 실패/0건 — 게시판 구조 변경 여부 확인 필요")
        # 사이트는 매 run 재생성. LLM 요약·일간 브리핑·블로그 초안은
        # 하루 한 번뿐인 DAILY_SLOT 실행에만 붙인다
        try:
            build_site(store, cfg.site, use_llm=(slot == DAILY_SLOT))
        except Exception as exc:  # noqa: BLE001
            log.exception("사이트 빌드 실패")
            errors.append(redact_secrets(f"site_build: {exc}", cfg.secrets.values()))
        if slot == DAILY_SLOT:
            try:
                day = datetime.now(KST).date()
                content = build_draft(store, day, use_llm=True, site_url=cfg.site.base_url)
                write_draft(content, day)
            except Exception as exc:  # noqa: BLE001
                log.exception("블로그 초안 생성 실패")
                errors.append(redact_secrets(f"blog_draft: {exc}", cfg.secrets.values()))

    if errors and not args.dry_run:
        summary = "\n".join(f"· {e}" for e in errors[:15])
        _notify(cfg, tg, f"collect(slot={slot}) 일부 실패 {len(errors)}건:\n{summary}")
    elif errors:
        log.info("[dry-run] 오류 %d건 — 알림 생략", len(errors))
    log.info("완료: %d건 게시, %d건 오류", posted_total, len(errors))
    if tg is not None:
        tg.close()
    store.close()
    # 부분 실패는 성공으로 처리 — 전 소스 실패 시에만 실패 종료
    all_failed = bool(sources) and all(not r.ok for r in health_results)
    if not args.dry_run:
        write_github_summary(_collect_summary(slot, posting, posted_total, health_results,
                                              errors, notices))
    return 1 if all_failed else 0


def _collect_summary(slot: str, posting: bool, posted: int, results: list[SourceResult],
                     errors: list[str], notices: list[str]) -> str:
    """GitHub Actions 요약 탭용 실행 리포트 (마스킹된 문자열만 들어온다)."""
    lines = [f"# collect 실행 요약 — slot: {slot}", ""]
    for n in notices:
        lines.append(f"> ⚠️ {n}")
    lines += [
        "",
        f"- 텔레그램 게시: {'활성' if posting else '비활성'} / 게시 {posted}건",
        f"- 소스 결과: 성공 {sum(1 for r in results if r.ok)} / 실패 {sum(1 for r in results if not r.ok)}",
        "",
        "| 소스 | 상태 | 수집 건수 | 비고 |", "|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r.name} (`{r.source_id}`) | {'✅' if r.ok else '❌'} | {r.count} | {r.error} |")
    if errors:
        lines += ["", "## 오류", ""] + [f"- {e}" for e in errors[:20]]
    return "\n".join(lines)


def run_verify(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    ctx = _make_ctx(cfg, "morning")
    ctx.advance_cursor = False  # 검증이 수집 워터마크를 움직이면 안 됨
    enabled = [s for s in cfg.sources if s.enabled]
    report = verify_sources(enabled, ctx)
    markdown = render_report(report)
    print(markdown)
    write_github_summary(markdown)
    # update_health는 호출하지 않음 — 연속 실패 카운터는 collect 전용 (3연속 경고 무결성)
    ctx.seen_store.close()
    ok = sum(1 for r in report.results if r.ok)
    log.info("검증: %d/%d 소스 성공", ok, len(report.results))
    return 0 if ok else 1


_G2B_HINTS = {
    "nokey": "DATA_GO_KR_KEY 미등록 — docs/setup-datago.md 참조",
    "direct": "GitHub 러너에서 나라장터에 직접 접속됩니다. 추가 설정 없이 입찰 수집이 동작합니다.",
    "relay": "직접 접속은 막혔지만 릴레이 경유로 성공했습니다. 현 설정 유지.",
    "fail": (
        "나라장터 접속 실패. 아래 사유를 확인하세요.\n"
        "- 키 발급 직후라면 서버 동기화에 최대 1시간이 걸립니다. 잠시 뒤 재시도\n"
        "- 한국 IP(브라우저)에서는 되는데 여기서만 실패하면 GitHub 러너 IP 차단입니다. "
        "docs/setup-worker-relay.md 대로 Cloudflare Worker 릴레이를 배포하고 "
        "G2B_RELAY_URL·G2B_RELAY_SECRET을 등록하세요."
    ),
}


def run_smoke_g2b(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    ctx = _make_ctx(cfg, "morning")
    mode = narajangteo.smoke_test(ctx)
    print(f"g2b mode: {mode}")
    lines = [f"# smoke-g2b 결과: `{mode}`", "", _G2B_HINTS.get(mode, "")]
    if ctx.g2b_errors:
        lines += ["", "## 실패 사유", *(f"- {e}" for e in ctx.g2b_errors)]
    for e in ctx.g2b_errors:
        print(e)
    write_github_summary("\n".join(lines))
    ctx.seen_store.close()
    return 0 if mode in ("direct", "relay") else 2


def run_blog_draft(args: argparse.Namespace) -> int:
    cfg = load_config()
    day = date.fromisoformat(args.date) if args.date else datetime.now(KST).date()
    store = SeenStore()
    content = build_draft(store, day, use_llm=True, site_url=cfg.site.base_url)
    path = write_draft(content, day)
    store.close()
    print(f"초안 생성: {path}")
    return 0


def _outbound_links(page_url: str, html: str) -> list[str]:
    """페이지 밖(다른 호스트)으로 나가는 링크 + 첨부 링크를 앵커 텍스트와 함께.

    협회 게시판은 원 기관 공고를 옮겨 싣는 경우가 많아, 상세 페이지 안에
    원문 URL이 숨어 있다. 그 URL을 찾아 링크를 원 기관으로 돌리기 위한 진단.
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlsplit

    host = urlsplit(page_url).netloc.lower().removeprefix("www.")
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for a in soup.find_all("a"):
        raw = (a.get("href") or "").strip()
        click = (a.get("onclick") or "").strip()
        cand = raw
        if not cand.lower().startswith(("http://", "https://")):
            m = re.search(r"""https?://[^'"\s)]+""", click)
            cand = m.group(0) if m else ("" if not raw or raw.startswith(("#", "javascript:")) else urljoin(page_url, raw))
        if not cand.lower().startswith(("http://", "https://")):
            continue
        other = urlsplit(cand).netloc.lower().removeprefix("www.")
        attach = bool(re.search(r"download|file|attach|fileDown", cand, re.I))
        if other == host and not attach:
            continue
        label = "첨부" if (other == host and attach) else "외부"
        found.append(f"  [{label}] {cand}  | {a.get_text(' ', strip=True)[:60]}")
    # 본문 텍스트에 그냥 적혀 있는 URL (링크가 아닌 경우)
    body = re.sub(r"<[^>]+>", " ", html)
    for m in dict.fromkeys(re.findall(r"https?://[^\s<>\"']+", body)):
        if urlsplit(m).netloc.lower().removeprefix("www.") != host:
            found.append(f"  [본문텍스트] {m}")
    return list(dict.fromkeys(found))


def _probe_url(url: str, cfg, out: list[str]) -> None:
    try:
        content, charset = fetch_bytes(url, timeout=30, verify_tls=False)
    except Exception as exc:  # noqa: BLE001
        out.append(f"요청 실패: {redact_secrets(str(exc), cfg.secrets.values())}")
        return
    text = decode_body(content, charset)
    out.append(f"OK — {len(content)} bytes, charset={charset}")
    links = re.findall(r"""(?:href|src)=["\']([^"\']+)""", text, re.I)
    feeds = [l for l in dict.fromkeys(links) if re.search(r"rss|feed|\.xml", l, re.I)]
    out.append("--- 피드/XML 링크 후보 ---")
    out += feeds[:40] or ["(없음)"]
    out.append("--- 외부/첨부 링크 후보 (원문 출처 추적용) ---")
    try:
        out += _outbound_links(url, text)[:40] or ["(없음)"]
    except Exception as exc:  # noqa: BLE001
        out.append(f"(추출 실패: {exc})")
    out.append("--- 본문 텍스트 앞부분 ---")
    out.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:6000])
    out.append("--- HTML 앞부분 ---")
    out.append(text[:3000])


def _probe_source(source, cfg, out: list[str]) -> None:
    ctx = _make_ctx(cfg, "morning")
    ctx.advance_cursor = False
    try:
        if source.type == "board":
            from bs4 import BeautifulSoup
            o = source.options
            url = str(o.get("list_url", "")).replace("{page}", "1")
            method = (o.get("method") or "GET").upper()
            data = ({k: str(v).replace("{page}", "1") for k, v in (o.get("form_data") or {}).items()}
                    if method == "POST" else None)
            if bool(o.get("via_curl", False)) and method == "GET":
                from .httpio import fetch_via_curl
                content, charset = fetch_via_curl(url, timeout=source.timeout), None
            else:
                content, charset = fetch_bytes(url, timeout=source.timeout, method=method, data=data,
                                               verify_tls=bool(o.get("verify_tls", True)))
            html = decode_body(content, charset, o.get("encoding", "auto"))
            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select(o.get("row_selector", ""))
            out.append(f"{url} → {len(content)} bytes, charset={charset}")
            out.append(f"row_selector={o.get('row_selector')!r} → {len(rows)}행 매칭")
            for row in rows[:3]:
                out.append("--- 매칭 행 ---")
                out.append(str(row)[:700])
            # 셀렉터가 몇 행을 잡았는지보다, 실제로 Item이 나오는지가 판정 기준이다.
            # 폼 테이블 한 줄을 잡고 "1행 매칭"이라 보고하면 성공으로 오해하기 쉽다.
            from .collectors.board import parse_list_page
            try:
                items = parse_list_page(html, source, base_url=url)
            except Exception as exc:  # noqa: BLE001
                out.append(f"파싱 실패: {exc}")
                items = []
            out.append(f"--- parse_list_page 결과: {len(items)}건 ---")
            for it in items[:10]:
                meta = " · ".join(f"{k}={v}" for k, v in (it.extra or {}).items())
                out.append(f"  {it.title[:60]} | {it.url[:110]}{' | ' + meta if meta else ''}")

            if len(items) < 5:
                # 어느 테이블을 겨냥해야 하는지 한눈에 보이게 — 셀렉터를 고칠 때 쓴다
                out.append("--- 테이블 목록 (행수 · class/id · 헤더) ---")
                for i, tbl in enumerate(soup.find_all("table")[:15]):
                    trs = tbl.find_all("tr")
                    heads = [th.get_text(" ", strip=True)[:14] for th in tbl.find_all("th")[:8]]
                    links = len(tbl.find_all("a"))
                    out.append(f"  table[{i}] tr={len(trs)} a={links} "
                               f"class={tbl.get('class')!r} id={tbl.get('id')!r} 헤더={heads}")
                out.append(f"구조 힌트: table={len(soup.find_all('table'))} ul={len(soup.find_all('ul'))} "
                           f"li={len(soup.find_all('li'))} a={len(soup.find_all('a'))}")
                out.append("--- a 태그 샘플 (최대 40) ---")
                for a in soup.find_all("a")[:40]:
                    out.append(f"  href={a.get('href')!r} onclick={a.get('onclick')!r} "
                               f"class={a.get('class')!r} | {a.get_text(' ', strip=True)[:60]}")
        else:
            items = COLLECTORS[source.type](source, ctx)
            out.append(f"{source.type} '{source.id}': {len(items)}건 수집")
            for it in items[:10]:
                out.append(f"  {it.dedup_key} | {it.title[:70]} | {it.url}")
    except Exception as exc:  # noqa: BLE001
        out.append(f"실패: {redact_secrets(str(exc), cfg.secrets.values())}")
    finally:
        ctx.seen_store.close()


def run_probe(args: argparse.Namespace) -> int:
    """소스 튜닝용 진단: URL이면 응답·피드 링크·본문 앞부분, 소스 id면 셀렉터 매칭/수집 결과.

    콤마로 여러 대상을 한 번에 지정할 수 있다 (URL 후보 여러 개를 한 실행으로 시험).
    """
    targets = [t.strip() for t in (args.target or "").split(",") if t.strip()]
    if not targets:
        print("probe --target 에 URL 또는 소스 id를 지정하세요 (콤마로 여러 개 가능)")
        return 2
    cfg = load_config()
    out: list[str] = []
    rc = 0
    for target in targets:
        if len(targets) > 1:
            out.append(f"\n===== {target} =====")
        if target.startswith(("http://", "https://")):
            _probe_url(target, cfg, out)
            continue
        source = next((s for s in cfg.sources if s.id == target), None)
        if source is None:
            out.append(f"소스 id '{target}' 없음. 사용 가능: {', '.join(s.id for s in cfg.sources)}")
            rc = 2
            continue
        _probe_source(source, cfg, out)
    text = "\n".join(out)
    print(text)
    write_github_summary(f"# probe: {', '.join(targets)}\n\n```\n{text[:60000]}\n```")
    write_run_log(f"probe: {', '.join(targets)}", text)
    return rc


def run_digest_preview(args: argparse.Namespace) -> int:
    """최근 수집분으로 다이제스트를 만들어 스테이징에 보낸다 (상태 변경 없음).

    게시 형식을 바꾼 뒤 실제 모양을 확인할 때 쓴다. 신규 항목이 없어도
    동작하고, posted/pending 상태를 건드리지 않아 몇 번을 돌려도 안전하다.
    """
    cfg = load_config()
    store = SeenStore()
    rows = store.recent(days=args.days, statuses=("posted", "seen", "pending", "skipped"))
    items = [_item_from_row(r) for r in rows]
    items = [it for it in items if it.title and it.url]
    if not items:
        print(f"최근 {args.days}일 수집분이 없습니다")
        store.close()
        return 2

    try:
        quotes = fetch_quotes(cfg.stocks, cfg.secrets)
    except Exception as exc:  # noqa: BLE001
        print(f"시세 조회 실패(본문만 미리보기): {str(exc)[:200]}")
        quotes = ([], [])
    msgs = format_daily_digest(
        items, datetime.now(KST).date(), cfg.site.base_url,
        title=cfg.site.title, quotes=quotes,
    )
    preview = "\n".join(msgs)
    print(f"{len(items)}건 → 메시지 {len(msgs)}통 / {len(preview)}자")
    write_github_summary(f"# 다이제스트 미리보기\n\n```\n{preview[:60000]}\n```")

    tg, staging_chat = _telegram(cfg)
    if tg is None:
        print("텔레그램 미설정 — 위 Summary로만 확인하세요")
        store.close()
        return 0
    for i, msg in enumerate(msgs, 1):
        marker = f"<i>[미리보기 {i}/{len(msgs)}]</i>\n"
        try:
            tg.send_message(staging_chat, marker + msg, {"is_disabled": True})
        except TelegramError as exc:
            print(f"전송 실패: {redact_secrets(str(exc), cfg.secrets.values())}")
            store.close()
            return 1
    print(f"스테이징 채널로 {len(msgs)}통 전송 완료")
    store.close()
    return 0


def write_run_log(title: str, text: str) -> None:
    """진단 결과를 state/에 남긴다 — 워크플로가 커밋하므로 저장소에서 다시 읽을 수 있다.

    Actions 로그 본문은 API로 꺼내기 어려워 진단 결과가 화면에만 남고 사라졌다.
    """
    path = Path("state/probe-latest.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {title}\n\n{text}\n", encoding="utf-8")


def run_smoke_stocks(args: argparse.Namespace) -> int:  # noqa: ARG001
    """종목 시세만 따로 조회해 본다 — 국내는 활용신청, 미국은 외부 접근성 확인용."""
    cfg = load_config()
    errors: list[str] = []
    kr, us = fetch_quotes(cfg.stocks, cfg.secrets, errors=errors)
    lines = ["| 구분 | 종목 | 종가 | 등락 | 기준일 |", "|---|---|---|---|---|"]
    for label, quotes in (("국내", kr), ("미국", us)):
        for q in quotes:
            pct = "―" if q.change_pct is None else f"{q.change_pct:+.2f}%"
            lines.append(f"| {label} | {q.name} | {q.close:,.2f} | {pct} | {q.as_of} |")
    want_kr = len(cfg.stocks.get("kr") or [])
    want_us = len(cfg.stocks.get("us") or [])
    summary = f"국내 {len(kr)}/{want_kr}종목, 미국 {len(us)}/{want_us}종목 조회 성공"

    # 다음에 할 일을 결과에 같이 적는다 — 표만 보고 무엇이 빠졌는지 알기 어렵다
    todo: list[str] = []
    if want_kr and not kr:
        todo.append("국내: data.go.kr에서 '금융위원회_주식시세정보'(15094808) 활용신청 "
                    "— DATA_GO_KR_KEY는 그대로 쓴다 (docs/setup-datago.md)")
    if want_us and not us:
        todo.append("미국: finnhub.io 무료 가입 후 FINNHUB_API_KEY Secret 등록")

    body = summary + "\n\n" + "\n".join(lines)
    if todo:
        body += "\n\n할 일\n" + "\n".join(f"- {t}" for t in todo)
    if errors:
        body += "\n\n실패 사유\n" + "\n".join(f"- {e}" for e in errors)
    print(body)
    write_github_summary(f"# smoke-stocks\n\n{body}")
    write_run_log("smoke-stocks", body)
    # 진단 커맨드는 워크플로를 실패시키지 않는다.
    # '키가 아직 없음'은 정상적인 설정 단계이지 고장이 아니다 — 실패로 알리면
    # "All jobs have failed" 메일이 날아가고, 진짜 고장 났을 때 무시하게 된다.
    # 판정은 위 표와 '할 일'을 사람이 읽는다.
    return 0


def run_build_site(args: argparse.Namespace) -> int:  # noqa: ARG001
    cfg = load_config()
    store = SeenStore()
    build_site(store, cfg.site, use_llm=False)
    store.close()
    print("site/ 재생성 완료")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="수집 → 스테이징 게시 → 사이트 빌드")
    p_collect.add_argument("--slot", choices=["morning", "noon", "evening"])
    p_collect.add_argument("--sources", help="콤마 구분 소스 id 필터")
    p_collect.add_argument("--dry-run", action="store_true", help="수집만, 상태 저장·게시 없음")
    p_collect.add_argument("--no-post", action="store_true", help="게시 없이 상태만 저장")
    p_collect.set_defaults(func=run_collect)

    sub.add_parser("verify-sources", help="전 소스 접근성 검증 리포트").set_defaults(func=run_verify)
    sub.add_parser("smoke-g2b", help="나라장터 direct/relay 판별").set_defaults(func=run_smoke_g2b)
    sub.add_parser("smoke-stocks", help="건설 관련 종목 시세 조회 확인").set_defaults(func=run_smoke_stocks)

    p_draft = sub.add_parser("blog-draft", help="네이버 블로그 초안 생성")
    p_draft.add_argument("--date", help="YYYY-MM-DD (기본: 오늘 KST)")
    p_draft.set_defaults(func=run_blog_draft)

    sub.add_parser("build-site", help="site/ 재생성").set_defaults(func=run_build_site)

    p_preview = sub.add_parser(
        "digest-preview", help="최근 수집분으로 다이제스트를 만들어 스테이징에 전송 (상태 변경 없음)")
    p_preview.add_argument("--days", type=int, default=1, help="최근 며칠분 (기본 1)")
    p_preview.set_defaults(func=run_digest_preview)

    p_probe = sub.add_parser("probe", help="소스 튜닝 진단: URL 응답/피드 링크 또는 소스 셀렉터 매칭 확인")
    p_probe.add_argument("--target", help="URL(http...) 또는 sources.yaml의 소스 id")
    p_probe.set_defaults(func=run_probe)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
