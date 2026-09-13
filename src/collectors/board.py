"""type: board — YAML 셀렉터로 선언되는 범용 HTML 게시판 크롤러.

sources.yaml 예:
    board:
      list_url: "https://.../list.asp?page={page}"   # {page}는 1부터 치환, 없으면 단일 페이지
      pages: 1
      method: GET            # GET | POST
      form_data: {}          # POST 파라미터 ({page} 치환 지원)
      encoding: auto         # auto | euc-kr | utf-8 ...
      verify_tls: true
      via_curl: false        # httpx가 TLS 협상에 실패하는 구형 서버용 (GET 전용)
      row_selector: "table.board tr:has(a)"
      skip_rows: 0
      url_base: "https://.../"          # 상대 href resolve 기준 (기본: list_url)
      id_pattern: "bidx=(\\d+)"         # natural key 추출 (실패 시 URL 해시 fallback)
      fields:
        title: {selector: "td.subject a", attr: text}
        link:  {selector: "td.subject a", attr: href}
        # onclick 게시판: {selector: "a", attr: onclick, pattern: "fnView\\('(\\d+)'\\)",
        #                  template: "https://.../view.do?id={value}"}
        date:  {selector: "td.date", attr: text, date_format: "%Y-%m-%d"}   # 옵션
        org:   {selector: "td.writer", attr: text}                           # 옵션
      # 제목 앞 [태그] 처리 (옵션) — 게시판 분류 태그는 버리고 남은 태그를 기관명으로
      title_tags: {ignore: ["건축계소식"], as_org: true, strip: true}
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import SourceConfig
from ..httpio import decode_body, fetch_bytes, fetch_via_curl, normalize_url
from ..robots import can_fetch, wait_for_host
from ..models import Item
from .base import CollectError, RunContext

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))  # 한국 게시판 날짜는 KST 기준


def extract_field(row_el, spec: dict) -> str | None:
    """FieldSpec 해석: selector로 요소 선택 → attr 값 → pattern 추출 → template 치환."""
    selector = spec.get("selector")
    el = row_el.select_one(selector) if selector else row_el
    if el is None:
        return None
    attr = spec.get("attr", "text")
    if attr == "text":
        value = el.get_text(" ", strip=True)
    else:
        value = el.get(attr)
        if isinstance(value, list):
            value = " ".join(value)
    if not value:
        return None
    value = str(value).strip()
    pattern = spec.get("pattern")
    if pattern:
        m = re.search(pattern, value)
        if not m:
            return None
        value = m.group(1)
    template = spec.get("template")
    if template:
        value = template.replace("{value}", value)
    return value or None


_TITLE_TAG_RE = re.compile(r"^\s*\[\s*([^\[\]]{1,20})\s*\]\s*")
MAX_TITLE_TAGS = 3  # 제목 앞 태그가 이보다 많으면 우리가 아는 패턴이 아니다


def split_title_tags(title: str, spec: dict) -> tuple[str, str | None]:
    """제목 앞 [태그]를 떼어내 (제목, 기관명)로 나눈다.

    협회 게시판은 남의 기관 공고를 옮겨 싣고, 그 기관 이름이 제목 앞 대괄호에만
    남는다 — 실측: "[건축계소식] [창원시]창원시 도시계획위원회 위원 공개모집".
    게시판 분류 태그(ignore)를 건너뛴 첫 태그를 발주기관으로 본다.
    """
    tags: list[str] = []
    rest = title
    while len(tags) < MAX_TITLE_TAGS:
        m = _TITLE_TAG_RE.match(rest)
        if not m:
            break
        tags.append(m.group(1).strip())
        rest = rest[m.end():]
    if not tags:
        return title.strip(), None

    ignore = {str(x).strip() for x in (spec.get("ignore") or [])}
    org = None
    if spec.get("as_org", True):
        org = next((t for t in tags if t and t not in ignore), None)
    if not spec.get("strip", True):
        return title.strip(), org
    # 태그를 다 떼고 나면 빈 제목이 되는 글도 있다 — 그때는 원문을 지킨다
    return (rest.strip() or title.strip()), org


def _parse_date(text: str, date_format: str | None) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    candidates = [date_format] if date_format else []
    # 흔한 한국 게시판 포맷들 폴백
    candidates += ["%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%y-%m-%d", "%y.%m.%d"]
    for fmt in candidates:
        if not fmt:
            continue
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    # "2026.08.29 14:00" 같은 꼬리 제거 후 재시도
    m = re.match(r"(\d{2,4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        if y < 100:
            y += 2000
        try:
            return datetime(y, mo, d, tzinfo=KST)
        except ValueError:
            return None
    return None


def parse_list_page(html: str, source: SourceConfig, base_url: str) -> list[Item]:
    cfg = source.options
    soup = BeautifulSoup(html, "html.parser")
    row_selector = cfg.get("row_selector")
    if not row_selector:
        raise CollectError(f"'{source.id}': board.row_selector 미설정")
    rows = soup.select(row_selector)
    fields: dict = cfg.get("fields") or {}
    if "title" not in fields or "link" not in fields:
        raise CollectError(f"'{source.id}': board.fields.title/link 필수")
    skip = int(cfg.get("skip_rows", 0))
    id_pattern = cfg.get("id_pattern")
    url_base = cfg.get("url_base") or base_url

    items: list[Item] = []
    for row in rows[skip:]:
        title = extract_field(row, fields["title"])
        link = extract_field(row, fields["link"])
        if not title or not link:
            continue
        url = normalize_url(urljoin(url_base, link))
        if not url:  # javascript: 등 비 http(s) 링크
            continue
        natural_key = None
        if id_pattern:
            m = re.search(id_pattern, url) or re.search(id_pattern, link)
            if m:
                natural_key = m.group(1)
        extra: dict = {}
        published_at = None
        if "date" in fields:
            date_text = extract_field(row, fields["date"])
            if date_text:
                published_at = _parse_date(date_text, fields["date"].get("date_format"))
        if "org" in fields:
            org = extract_field(row, fields["org"])
            if org:
                extra["org"] = org
        if cfg.get("title_tags"):
            title, tag_org = split_title_tags(title, cfg["title_tags"])
            # 목록에 글쓴이 칸이 따로 있으면 그쪽이 더 정확하다
            if tag_org and not extra.get("org"):
                extra["org"] = tag_org
        items.append(Item(
            source_id=source.id,
            category=source.category,
            title=title,
            url=url,
            natural_key=natural_key,
            key_prefix=f"board:{source.id}",
            published_at=published_at,
            author=source.name,
            extra=extra,
        ))
    return items


def collect(source: SourceConfig, ctx: RunContext) -> list[Item]:
    cfg = source.options
    list_url = cfg.get("list_url")
    if not list_url:
        raise CollectError(f"'{source.id}': board.list_url 미설정")
    pages = int(cfg.get("pages", 1))
    method = (cfg.get("method") or "GET").upper()
    encoding = cfg.get("encoding", "auto")
    verify_tls = bool(cfg.get("verify_tls", True))
    respect_robots = bool(cfg.get("respect_robots", True))
    # 구형 서버 일부는 httpx의 TLS 협상을 끊어버린다("Server disconnected") —
    # 나라장터에서 검증된 curl 경로를 게시판에도 열어둔다. GET에만 의미가 있다.
    via_curl = bool(cfg.get("via_curl", False)) and method == "GET"
    if not verify_tls:
        log.warning("'%s': TLS 검증 비활성 (구형 인증서 사이트)", source.id)

    items: list[Item] = []
    for page in range(1, pages + 1):
        url = list_url.replace("{page}", str(page))
        form_data = None
        if method == "POST":
            form_data = {
                k: str(v).replace("{page}", str(page))
                for k, v in (cfg.get("form_data") or {}).items()
            }
        if respect_robots and not can_fetch(url, verify_tls=verify_tls):
            raise CollectError(
                f"'{source.id}': robots.txt가 수집을 금지함 ({url}). "
                "정책이 바뀐 것이라면 소스를 비활성화하거나 "
                "board.respect_robots: false로 명시적으로 해제할 것"
            )
        if respect_robots:
            wait_for_host(url)
        try:
            if via_curl:
                content, charset = fetch_via_curl(url, timeout=source.timeout), None
            else:
                content, charset = fetch_bytes(
                    url, timeout=source.timeout, verify_tls=verify_tls,
                    method=method, data=form_data,
                )
        except Exception as exc:  # noqa: BLE001
            raise CollectError(f"'{source.id}' p{page} 요청 실패: {exc}") from exc
        html = decode_body(content, charset, encoding)
        items.extend(parse_list_page(html, source, base_url=url))
    return items
