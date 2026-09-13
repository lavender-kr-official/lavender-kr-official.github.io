"""type: eminwon — 새올(eminwon) 표준 고시공고 게시판 프리셋.

전국 기초지자체 수백 곳이 같은 새올 행정시스템 게시판을 쓰므로 host만 바꿔 재사용.
sources.yaml 예:
    eminwon:
      host: eminwon.gijang.go.kr
      se_codes: ["01", "02"]        # 고시/공고 구분코드
      page_size: 30
      # 아래는 배포 후 실측으로 조정 가능한 오버라이드
      list_path: "/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do"
      form_data: {...}              # 기본 폼 병합·덮어쓰기
      anchor_pattern: "(\\d{5,})"
      detail_template: "https://{host}/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do?method=selectOfrNotAncmt&not_ancmt_mgt_no={value}"
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..config import SourceConfig
from ..httpio import decode_body, fetch_bytes
from ..robots import can_fetch, wait_for_host
from ..models import Item
from .base import CollectError, RunContext

DEFAULT_LIST_PATH = "/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do"
DEFAULT_DETAIL_TEMPLATE = (
    "https://{host}/emwp/gov/mogaha/ntis/web/ofr/action/OfrAction.do"
    "?jndinm=OfrNotAncmtEJB&context=NTIS&method=selectOfrNotAncmt"
    "&methodnm=selectOfrNotAncmtRegst&not_ancmt_mgt_no={value}"
)
# 목록 행의 상세 링크가 대개 javascript:searchDetail('12345') 형태
DEFAULT_ANCHOR_PATTERN = r"(?:searchDetail|not_ancmt_mgt_no)[^0-9]{0,5}(\d{4,})"


def default_form(se_codes: list[str], page_size: int) -> dict:
    return {
        "jndinm": "OfrNotAncmtEJB",
        "context": "NTIS",
        "method": "selectListOfrNotAncmt",
        "methodnm": "selectListOfrNotAncmtHomepage",
        "homepage_pbs_yn": "Y",
        "subCheck": "Y",
        "ofr_pageSize": str(page_size),
        "not_ancmt_se_code": ",".join(se_codes),
        "Key": "B_Subject",
        "pageIndex": "1",
    }


def parse_list_html(html: str, source: SourceConfig, host: str) -> list[Item]:
    cfg = source.options
    anchor_pattern = cfg.get("anchor_pattern") or DEFAULT_ANCHOR_PATTERN
    detail_template = cfg.get("detail_template") or DEFAULT_DETAIL_TEMPLATE
    soup = BeautifulSoup(html, "html.parser")
    items: list[Item] = []
    seen_nos: set[str] = set()
    for a in soup.find_all("a"):
        target = " ".join(
            str(a.get(attr) or "") for attr in ("href", "onclick")
        )
        m = re.search(anchor_pattern, target)
        if not m:
            continue
        no = m.group(1)
        title = a.get_text(" ", strip=True)
        if not title or no in seen_nos:
            continue
        seen_nos.add(no)
        url = detail_template.replace("{host}", host).replace("{value}", no)
        items.append(Item(
            source_id=source.id,
            category=source.category,
            title=title,
            url=url,
            natural_key=no,
            key_prefix=f"emw:{host}",
            author=source.name,
        ))
    return items


def collect(source: SourceConfig, ctx: RunContext) -> list[Item]:
    cfg = source.options
    host = cfg.get("host")
    if not host:
        raise CollectError(f"'{source.id}': eminwon.host 미설정")
    se_codes = [str(c) for c in (cfg.get("se_codes") or ["01", "02"])]
    page_size = int(cfg.get("page_size", 30))
    list_path = cfg.get("list_path") or DEFAULT_LIST_PATH
    form = default_form(se_codes, page_size)
    form.update({k: str(v) for k, v in (cfg.get("form_data") or {}).items()})
    url = f"https://{host}{list_path}"
    # 지자체 수백 곳으로 늘어날 수집기다 — board와 같은 규칙을 지켜야 한다.
    # 사이트가 거부하면 조용히 건너뛰지 않고 알린다: 정책 변경은 사람이 봐야 한다.
    respect_robots = bool(cfg.get("respect_robots", True))
    verify_tls = bool(cfg.get("verify_tls", True))
    if respect_robots:
        if not can_fetch(url, verify_tls=verify_tls):
            raise CollectError(
                f"'{source.id}': robots.txt가 수집을 금지함 ({host}). "
                "소스를 비활성화하거나 eminwon.respect_robots: false로 명시적으로 해제할 것"
            )
        wait_for_host(url)
    try:
        content, charset = fetch_bytes(
            url, timeout=source.timeout, method="POST", data=form,
            verify_tls=verify_tls,
        )
    except Exception as exc:  # noqa: BLE001
        raise CollectError(f"'{source.id}' 요청 실패: {exc}") from exc
    html = decode_body(content, charset, cfg.get("encoding", "auto"))
    # 공고가 없는 날도 정상 — 0건은 source_health의 연속 카운터가 잡는다
    return parse_list_html(html, source, host)
