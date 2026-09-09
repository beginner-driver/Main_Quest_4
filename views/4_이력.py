"""④ 이력 — 쌓인 이슈를 되짚어본다.

DB에 계속 쌓이므로 거의 공짜로 딸려오는 화면이다.
스레드 타임라인은 '이 사안이 며칠에 걸쳐 어떻게 전개됐나'를 보여준다.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402

st.title("🗓 이력")

MARK = {"최우선": "★", "필수": "·", "참고": " "}


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

f = st.columns([2, 1, 2])
name = f[0].selectbox("키워드 세트", [s["name"] for s in sets])
days = f[1].selectbox("기간", [7, 14, 30, 90, 365], index=2, format_func=lambda d: f"최근 {d}일")
grades = f[2].multiselect("중요도", ["최우선", "필수", "참고"], default=["최우선", "필수"])

sid = db.get_set(conn, name)["id"]
threads = db.recent_threads(conn, sid, days=days)
q = st.text_input("제목 검색", placeholder="예: 기본법")
if q:
    threads = [t for t in threads if q in t["title"]]
if grades:
    threads = [t for t in threads if t["importance"] in grades]

st.caption(f"{len(threads)}개 이슈")
if not threads:
    st.info("조건에 맞는 이슈가 없습니다.")
    st.stop()

st.dataframe(
    pd.DataFrame([{
        "중요도": t["importance"], "라벨": t["label"], "이슈": t["title"],
        "기사": t["item_count"], "최초": t["first_seen"], "최종": t["last_seen"],
        "상태": t["status"],
    } for t in threads]),
    hide_index=True, width="stretch",
)

st.markdown("### 스레드 타임라인")
pick = st.selectbox(
    "이슈 선택", [t["id"] for t in threads],
    format_func=lambda i: next(
        f"{MARK.get(t['importance'], ' ')} {t['title']}" for t in threads if t["id"] == i))

t = next(x for x in threads if x["id"] == pick)
c = st.columns(4)
c[0].metric("중요도", t["importance"])
c[1].metric("라벨", t["label"])
c[2].metric("기사", f"{t['item_count']}건")
span = (pd.Timestamp(t["last_seen"]) - pd.Timestamp(t["first_seen"])).days + 1
c[3].metric("경과", f"{span}일")

if t["reason"]:
    st.info(f"판단 근거: {t['reason']}")
if t["agent_importance"] and t["agent_importance"] != t["importance"]:
    st.caption(f"에이전트 원안은 '{t['agent_importance']}'였고 사람이 "
               f"'{t['importance']}'으로 고쳤습니다.")

rows = db.thread_items(conn, pick)
st.dataframe(
    pd.DataFrame([{
        "게시일시": r["published_at"], "제목": r["title"], "언론사": r["press"],
        "요약": r["description"], "링크": r["link"],
    } for r in rows]),
    hide_index=True, width="stretch",
    column_config={"링크": st.column_config.LinkColumn(display_text="열기")},
)
