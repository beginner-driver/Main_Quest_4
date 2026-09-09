"""도구 4개. 입출력 스키마(PRD §4.2)와 실패 처리 규칙(§4.3)이 여기 있다.

권한 최소화: 에이전트에게 노출되는 DB 도구는 읽기 전용이다.
쓰기(라벨 확정)는 사람의 승인이 트리거하며 db.confirm_thread()가 수행한다.
"""
import os
import re
import time

import requests

from . import collect, db

NAVER_HOST = "n.news.naver.com"

# 에이전트에게 보여주는 도구 정의. description은 '언제 써야 하는가'를 말한다.
TOOLS = [
    {
        "name": "search_news",
        "description": (
            "네이버 뉴스 검색 API로 키워드에 해당하는 기사를 수집한다. 지정한 시간 범위 내 "
            "발행 기사만 반환한다. 키워드는 OR 조건으로 각각 개별 검색 후 합친다. 수집 "
            "단계에서 자동 호출되며, 판정 중 추가 확인이 필요할 때만 재호출한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {"type": "array", "items": {"type": "string"}},
                "hours": {"type": "integer", "default": 24},
                "max_per_kw": {"type": "integer", "default": 300},
            },
            "required": ["keywords"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_history",
        "description": (
            "현재 키워드 세트의 과거 이슈 스레드를 조회한다. 지금 판정 중인 묶음이 이미 "
            "다뤄진 이슈의 후속인지 완전히 새로운 건인지 판단할 때 사용한다. 신규/후속 "
            "판정 전에 반드시 호출한다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword_set_id": {"type": "integer"},
                "days": {"type": "integer", "default": 14},
                "query": {"type": "string"},
            },
            "required": ["keyword_set_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fetch_article",
        "description": (
            "기사 본문을 가져온다. 제목과 description만으로 중요도를 판단할 수 없을 때만 "
            "사용한다. 네이버 description은 80~100자로 잘려 있어 '무엇이 어떻게 바뀌었는지'가 "
            "누락되는 경우가 있다. 특히 법·제도 변경(최우선 등급 후보)인데 개정 내용이 "
            "불명확할 때 호출한다. 느리고 실패율이 있어 실행당 최대 3건으로 제한된다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "reason": {"type": "string",
                           "description": "왜 본문이 필요한지. 실행 로그에 남아 사람이 검토한다"},
            },
            "required": ["item_id", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "export_excel",
        "description": "확정된 이슈 목록을 엑셀로 출력한다. 사람의 승인 이후에만 호출 가능하다.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "integer"},
                "include_all": {"type": "boolean", "default": True},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
]


class ToolAbort(Exception):
    """복구 불가. 진행하면 결과가 오염되므로 실행 전체를 중단한다."""


# --- search_news -------------------------------------------------------------

def search_news(keywords, hours=24, max_per_kw=300, **kw):
    """실패 규칙: 키워드 단위 격리, 상한은 경고로 기록하고 중단하지 않는다."""
    return collect.search_news(
        keywords, hours=hours, max_per_kw=max_per_kw,
        client_id=os.getenv("NAVER_CLIENT_ID"),
        client_secret=os.getenv("NAVER_CLIENT_SECRET"),
        **kw,
    )


# --- lookup_history ----------------------------------------------------------

def lookup_history(conn, keyword_set_id, days=14, query=None):
    """실패 규칙: 즉시 중단.

    이력 없이 판정하면 전건이 '신규'로 잘못 라벨링되고, 그 결과가 확정되면
    다음날 판단까지 연쇄로 오염된다. 품질 저하로 흡수할 수 있는 실패가 아니다.
    """
    try:
        return db.recent_threads(conn, keyword_set_id, days=days, query=query)
    except Exception as e:
        raise ToolAbort(f"이력 조회 실패 — 판정을 중단합니다: {e}") from e


# --- fetch_article -----------------------------------------------------------

# ponytail: 네이버 뉴스 본문 컨테이너 하나만 본다. 언론사 원문까지 필요해지면
#           사이트별 파서로 확장한다. 지금은 실패하면 description으로 판정한다.
_BODY = re.compile(
    r'<(?:article|div)[^>]*id="(?:dic_area|newsct_article)"[^>]*>(.*?)</(?:article|div)>',
    re.S,
)
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S)


def fetch_article(conn, item_id, reason, timeout=8, get=None):
    """실패 규칙: 재시도 없음. 실패해도 description으로 판정하고 진행한다."""
    get = get or (lambda u, **k: requests.get(u, **k))
    rows = db.get_items(conn, [item_id])
    if not rows:
        return {"success": False, "content": "", "error": "parse_failed"}

    link = rows[0]["link"] or ""
    if NAVER_HOST not in link:
        # 언론사 원문은 구조가 제각각이라 시도하지 않는다
        return {"success": False, "content": "", "error": "no_naver_link"}

    try:
        r = get(link, timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 (compatible; IssueRadar/1.0)"})
        r.raise_for_status()
        html_text = r.text
    except Exception:
        return {"success": False, "content": "", "error": "timeout"}

    m = _BODY.search(html_text)
    if not m:
        return {"success": False, "content": "", "error": "parse_failed"}

    body = collect.clean(_SCRIPT.sub(" ", m.group(1)))
    body = re.sub(r"\s+", " ", body).strip()
    return {"success": True, "content": body[:4000], "error": ""}


# --- export_excel ------------------------------------------------------------

def export_excel(conn, run_id, include_all=True, out_dir="exports"):
    """승인 이후에만 호출된다. 실패해도 DB 확정 상태는 유지된다."""
    import pandas as pd

    run = db.get_run(conn, run_id)
    if not run:
        raise ValueError(f"run {run_id} 없음")

    order = {"최우선": 0, "필수": 1, "참고": 2}
    rows = []
    threads = db.recent_threads(conn, run["keyword_set_id"], days=3650)
    threads = [t for t in threads if t.get("run_id") == run_id]
    threads.sort(key=lambda t: (order.get(t["importance"], 9), t["title"]))

    for t in threads:
        if not include_all and t["importance"] == "참고":
            continue
        for it in db.thread_items(conn, t["id"]):
            rows.append({
                "중요도": t["importance"], "라벨": t["label"], "이슈": t["title"],
                "근거": t["reason"], "게시일시": it["published_at"],
                "제목": it["title"], "언론사": it["press"],
                "요약": it["description"], "링크": it["link"],
            })

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"issue_radar_run{run_id}.xlsx")
    df = pd.DataFrame(rows)
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="이슈")
        if not df.empty and "링크" in df.columns:
            ws = writer.sheets["이슈"]
            col = df.columns.get_loc("링크")
            for i, url in enumerate(df["링크"], start=1):
                if url:
                    ws.write_url(i, col, url, string=url)
    return {"file_path": path, "row_count": len(rows)}


# --- 실행 + 기록 -------------------------------------------------------------

def call(conn, run_id, seq, name, args, reason=""):
    """도구를 부르고 run_step에 남긴다. trace가 관찰 가능성의 전부다."""
    t0 = time.time()
    fn = {"search_news": lambda: search_news(**args),
          "lookup_history": lambda: lookup_history(conn, **args),
          "fetch_article": lambda: fetch_article(conn, **args),
          "export_excel": lambda: export_excel(conn, **args)}[name]
    try:
        result = fn()
    except ToolAbort as e:
        db.log_step(conn, run_id, seq, name, reason=reason, input_summary=str(args)[:200],
                    duration_ms=int((time.time() - t0) * 1000), success=False, error=str(e))
        raise
    except Exception as e:
        db.log_step(conn, run_id, seq, name, reason=reason, input_summary=str(args)[:200],
                    duration_ms=int((time.time() - t0) * 1000), success=False,
                    error=f"{type(e).__name__}: {e}")
        return None

    ok = not (isinstance(result, dict) and result.get("success") is False)
    db.log_step(conn, run_id, seq, name, reason=reason, input_summary=str(args)[:200],
                output_summary=_summarize(name, result),
                duration_ms=int((time.time() - t0) * 1000), success=ok,
                error="" if ok else result.get("error", ""))
    return result


def _summarize(name, result):
    if name == "search_news":
        s = f"{len(result['items'])}건"
        if result["truncated_keywords"]:
            s += f" / 상한도달: {','.join(result['truncated_keywords'])}"
        if result["failed_keywords"]:
            s += f" / 실패: {','.join(result['failed_keywords'])}"
        return s
    if name == "lookup_history":
        return f"스레드 {len(result)}건"
    if name == "fetch_article":
        return f"본문 {len(result['content'])}자" if result["success"] else "실패"
    if name == "export_excel":
        return f"{result['row_count']}행 → {result['file_path']}"
    return str(result)[:200]
