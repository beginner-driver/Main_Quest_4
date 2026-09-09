"""SQLite 접근. DB에 쓰는 유일한 경로다.

에이전트는 이 모듈의 읽기 함수만 도구로 노출받는다 (PRD §4.1 권한 최소화).
쓰기는 사람의 승인 액션이나 파이프라인 코드가 호출한다.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "radar.db"
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"


def connect(path=None):
    path = Path(path) if path else DB_PATH
    if path != Path(":memory:"):
        path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: check_same_thread=False. Streamlit은 리런마다 다른 스레드에서 돌아
    #           캐시된 커넥션을 재사용하면 ProgrammingError가 난다. 단일 사용자
    #           로컬 전제이며, 동시 쓰기가 생기면 커넥션 풀이나 쓰기 큐로 바꾼다.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _split(s):
    return [t for t in (s or "").split(",") if t.strip()]


# --- 키워드 세트 -------------------------------------------------------------

def get_or_create_set(conn, name, keywords, description="", criteria="", org_names=""):
    """keywords는 리스트 또는 쉼표 문자열. 이미 있으면 기존 id를 돌려준다."""
    if isinstance(keywords, (list, tuple)):
        keywords = ",".join(keywords)
    row = conn.execute("SELECT id FROM keyword_set WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO keyword_set (name, description, keywords, criteria, org_names)"
        " VALUES (?, ?, ?, ?, ?)",
        (name, description, keywords, criteria, org_names),
    )
    conn.commit()
    return cur.lastrowid


def get_set(conn, ident):
    """이름 또는 id로 세트를 가져온다. keywords/org_names는 리스트로 풀어서 준다."""
    col = "id" if isinstance(ident, int) else "name"
    row = conn.execute(f"SELECT * FROM keyword_set WHERE {col} = ?", (ident,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["keywords"] = _split(d["keywords"])
    d["org_names"] = _split(d["org_names"])
    return d


def list_sets(conn):
    return _rows(conn.execute("SELECT * FROM keyword_set ORDER BY name"))


# --- 항목 --------------------------------------------------------------------

def upsert_items(conn, items):
    """items 순서대로 item.id 리스트를 돌려준다.

    이미 있는 기사는 matched_keywords만 병합한다. 다른 키워드로 다시 걸린 경우다.
    """
    # ponytail: 건별 SELECT+INSERT. 수천 건으로 늘면 executemany + 일괄 조회로 바꾼다.
    ids = []
    for it in items:
        key = it["dedup_key"]
        row = conn.execute(
            "SELECT id, matched_keywords FROM item WHERE dedup_key = ?", (key,)
        ).fetchone()
        new_kws = it.get("matched_keywords") or []
        if isinstance(new_kws, str):
            new_kws = _split(new_kws)
        if row:
            merged = list(dict.fromkeys(_split(row["matched_keywords"]) + new_kws))
            if merged != _split(row["matched_keywords"]):
                conn.execute(
                    "UPDATE item SET matched_keywords = ? WHERE id = ?",
                    (",".join(merged), row["id"]),
                )
            ids.append(row["id"])
            continue
        cur = conn.execute(
            "INSERT INTO item (source, title, description, link, origin_link, press,"
            " published_at, dedup_key, matched_keywords) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                it.get("source", "naver_news"),
                it["title"],
                it.get("description", ""),
                it.get("link", ""),
                it.get("origin_link", ""),
                it.get("press", ""),
                it["published_at"],
                key,
                ",".join(new_kws),
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def get_items(conn, item_ids):
    if not item_ids:
        return []
    q = ",".join("?" * len(item_ids))
    return _rows(conn.execute(f"SELECT * FROM item WHERE id IN ({q})", tuple(item_ids)))


# --- 스레드 ------------------------------------------------------------------

def recent_threads(conn, keyword_set_id, days=14, query=None):
    """이 세트의 최근 스레드. 라벨 판정(§3.5)과 lookup_history 도구가 쓴다."""
    sql = (
        "SELECT t.*, COUNT(it.item_id) AS item_count FROM thread t"
        " LEFT JOIN item_thread it ON it.thread_id = t.id"
        " WHERE t.keyword_set_id = ?"
        "   AND t.last_seen >= date('now','localtime',?)"
    )
    params = [keyword_set_id, f"-{int(days)} days"]
    if query:
        sql += " AND t.title LIKE ?"
        params.append(f"%{query}%")
    sql += " GROUP BY t.id ORDER BY t.last_seen DESC"
    return _rows(conn.execute(sql, params))


def save_thread(conn, keyword_set_id, run_id, title, first_seen, last_seen,
                label="신규", importance="참고", reason="", status="judged",
                item_ids=(), thread_id=None):
    """새 스레드를 만들거나(thread_id=None) 기존 스레드를 갱신한다.

    에이전트 원안(agent_label/agent_importance)은 최초 1회만 기록한다.
    나중에 사람이 고친 값과 비교해 수정률을 낸다.
    """
    if thread_id is None:
        cur = conn.execute(
            "INSERT INTO thread (keyword_set_id, run_id, title, first_seen, last_seen,"
            " label, importance, reason, agent_label, agent_importance, status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (keyword_set_id, run_id, title, first_seen, last_seen,
             label, importance, reason, label, importance, status),
        )
        thread_id = cur.lastrowid
    else:
        conn.execute(
            "UPDATE thread SET run_id=?, last_seen=?, label=?, importance=?,"
            " reason=?, status=? WHERE id=?",
            (run_id, last_seen, label, importance, reason, status, thread_id),
        )
    for iid in item_ids:
        conn.execute(
            "INSERT OR IGNORE INTO item_thread (item_id, thread_id) VALUES (?,?)",
            (iid, thread_id),
        )
    conn.commit()
    return thread_id


def thread_items(conn, thread_id):
    return _rows(conn.execute(
        "SELECT i.* FROM item i JOIN item_thread it ON it.item_id = i.id"
        " WHERE it.thread_id = ? ORDER BY i.published_at DESC", (thread_id,)))


def confirm_thread(conn, thread_id, label, importance, reason=None):
    """사람의 승인. agent_* 는 건드리지 않아 수정 여부가 남는다."""
    if reason is None:
        conn.execute(
            "UPDATE thread SET label=?, importance=?, status='confirmed',"
            " confirmed_at=datetime('now','localtime') WHERE id=?",
            (label, importance, thread_id))
    else:
        conn.execute(
            "UPDATE thread SET label=?, importance=?, reason=?, status='confirmed',"
            " confirmed_at=datetime('now','localtime') WHERE id=?",
            (label, importance, reason, thread_id))
    conn.commit()


def revision_rate(conn, keyword_set_id, days=30):
    """최근 N일 수정률 (PRD §5.5). 확정된 스레드 중 사람이 값을 바꾼 비율."""
    row = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN label <> COALESCE(agent_label, label)"
        "        OR importance <> COALESCE(agent_importance, importance)"
        "      THEN 1 ELSE 0 END) AS revised"
        " FROM thread WHERE keyword_set_id = ? AND status = 'confirmed'"
        "   AND confirmed_at >= date('now','localtime',?)",
        (keyword_set_id, f"-{int(days)} days"),
    ).fetchone()
    total = row["total"] or 0
    revised = row["revised"] or 0
    return {"total": total, "revised": revised,
            "rate": (revised / total) if total else 0.0}


# --- 실행 기록 ---------------------------------------------------------------

def create_run(conn, keyword_set_id, hours=24):
    cur = conn.execute(
        "INSERT INTO run (keyword_set_id, hours) VALUES (?,?)", (keyword_set_id, hours))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status="done", **stats):
    allowed = {"item_count", "thread_count", "tokens_in", "tokens_out",
               "cost", "truncated", "failed"}
    cols, vals = [], []
    for k, v in stats.items():
        if k in allowed:
            cols.append(f"{k}=?")
            vals.append(",".join(v) if isinstance(v, (list, tuple)) else v)
    cols += ["status=?", "finished_at=datetime('now','localtime')"]
    vals.append(status)
    vals.append(run_id)
    conn.execute(f"UPDATE run SET {', '.join(cols)} WHERE id=?", vals)
    conn.commit()


def get_run(conn, run_id):
    row = conn.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def latest_run(conn, keyword_set_id):
    row = conn.execute(
        "SELECT * FROM run WHERE keyword_set_id=? ORDER BY id DESC LIMIT 1",
        (keyword_set_id,)).fetchone()
    return dict(row) if row else None


def log_step(conn, run_id, seq, tool, reason="", input_summary="",
             output_summary="", duration_ms=0, success=True, error=""):
    conn.execute(
        "INSERT INTO run_step (run_id, seq, tool, reason, input_summary,"
        " output_summary, duration_ms, success, error) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, seq, tool, reason, input_summary, output_summary,
         duration_ms, 1 if success else 0, error),
    )
    conn.commit()


def run_steps(conn, run_id):
    return _rows(conn.execute(
        "SELECT * FROM run_step WHERE run_id=? ORDER BY seq", (run_id,)))
