"""② 실행 로그 — 관찰 가능성 (PRD §7.2②).

에이전트가 어떤 도구를 왜 불렀고 무엇을 받았는지가 전부 여기 남는다.
'작업을 끝냈다'는 말과 실제로 끝난 것은 다르므로, 사람이 직접 확인할 수 있어야 한다.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402

st.title("🧾 실행 로그")


@st.cache_resource
def get_conn():
    c = db.connect()
    db.init_db(c)
    return c


conn = get_conn()
sets = db.list_sets(conn)
if not sets:
    st.warning("키워드 세트가 없습니다.")
    st.stop()

name = st.selectbox("키워드 세트", [s["name"] for s in sets])
sid = db.get_set(conn, name)["id"]

runs = [dict(r) for r in conn.execute(
    "SELECT * FROM run WHERE keyword_set_id=? ORDER BY id DESC LIMIT 50", (sid,))]
if not runs:
    st.info("실행 이력이 없습니다.")
    st.stop()

st.dataframe(
    pd.DataFrame([{
        "#": r["id"], "시작": r["started_at"], "상태": r["status"],
        "수집": r["item_count"], "묶음": r["thread_count"],
        "토큰": r["tokens_in"], "비용($)": round(r["cost"], 4),
        "상한도달": r["truncated"], "실패": r["failed"],
    } for r in runs]),
    hide_index=True, width="stretch",
)

pick = st.selectbox(
    "상세 보기", [r["id"] for r in runs],
    format_func=lambda i: f"실행 #{i}  ({next(r['started_at'] for r in runs if r['id'] == i)})")

run = db.get_run(conn, pick)
steps = db.run_steps(conn, pick)

c = st.columns(5)
c[0].metric("수집", f"{run['item_count']}건")
c[1].metric("묶음", f"{run['thread_count']}개")
c[2].metric("토큰", f"{run['tokens_in']:,}")
c[3].metric("비용", f"${run['cost']:.4f}")
c[4].metric("총 소요", f"{sum(s['duration_ms'] for s in steps) / 1000:.1f}초")

st.markdown("### 도구 호출 추적")
if not steps:
    st.info("기록된 단계가 없습니다.")

for s in steps:
    icon = "✅" if s["success"] else "❌"
    st.markdown(
        f"`{s['seq']:>2}` {icon} **{s['tool']}** "
        f"— {s['output_summary'] or s['error']}  ·  {s['duration_ms'] / 1000:.1f}초")
    if s["reason"]:
        st.caption(f"　　이유: {s['reason']}")
    if s["input_summary"]:
        st.caption(f"　　입력: {s['input_summary']}")
    if s["error"]:
        st.caption(f"　　오류: {s['error']} → 대체 경로로 진행")

st.markdown("### 병목")
if steps:
    slow = sorted(steps, key=lambda s: -s["duration_ms"])[:5]
    st.dataframe(
        pd.DataFrame([{"단계": s["seq"], "도구": s["tool"],
                       "소요(초)": round(s["duration_ms"] / 1000, 1),
                       "이유": s["reason"]} for s in slow]),
        hide_index=True, width="stretch")
