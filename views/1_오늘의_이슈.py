"""① 오늘의 이슈 — 메인 화면이자 승인 지점 (PRD §5.1).

원칙: 여기서 기사가 사라지지 않는다. 접히고 정렬될 뿐이다.
라벨과 중요도는 이미 채워진 상태로 나오고, 사람은 틀린 것만 고친다.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import agent, db, llm, tools  # noqa: E402
from main import load_env  # noqa: E402

GRADES = ["최우선", "필수", "참고"]
LABELS = ["신규", "후속", "기존"]
MARK = {"최우선": "★", "필수": "·", "참고": " "}

load_env()


@st.cache_resource
def get_conn():
    c = db.connect()
    db.init_db(c)
    return c


conn = get_conn()
sets = db.list_sets(conn)

if not sets:
    st.title("이슈 레이더")
    st.warning("키워드 세트가 없습니다. 터미널에서 `python main.py --init` 을 먼저 실행하세요.")
    st.stop()

# --- 현재 세트: 다른 주제 세트를 잘못 걸고 돌리면 결과가 통째로 이상해진다 -----
names = [s["name"] for s in sets]
top = st.columns([3, 1, 1])
with top[0]:
    name = st.selectbox("키워드 세트", names, label_visibility="collapsed")
    st.markdown(f"## 📡 {name}")
cur = db.get_set(conn, name)
with top[1]:
    hours = st.number_input("최근 (시간)", 1, 168, 24)
with top[2]:
    st.write("")
    go = st.button("수집 · 판정 실행", type="primary", width="stretch")

st.caption(" · ".join(cur["keywords"]))

if not llm.available():
    st.warning("Ollama가 실행 중이 아닙니다. 판정 단계에서 실패합니다.")

# --- 실행 -------------------------------------------------------------------
if go:
    with st.status("실행 중…", expanded=True) as status:
        st.write("네이버 뉴스 수집")
        try:
            rid = agent.run_agent(conn, cur["id"], hours=hours)
        except Exception as e:
            status.update(label="실패", state="error")
            st.exception(e)
            st.stop()
        st.write("판정 완료")
        status.update(label=f"실행 #{rid} 완료", state="complete")
    st.session_state["run_id"] = rid

run = (db.get_run(conn, st.session_state["run_id"])
       if st.session_state.get("run_id") else db.latest_run(conn, cur["id"]))

if not run:
    st.info("아직 실행 이력이 없습니다. 위 버튼을 눌러 첫 수집을 시작하세요.")
    st.stop()

# --- 실행 요약 + 경고 --------------------------------------------------------
m = st.columns(5)
m[0].metric("수집", f"{run['item_count']}건")
m[1].metric("묶음", f"{run['thread_count']}개")
m[2].metric("토큰", f"{run['tokens_in']:,}")
m[3].metric("비용", f"${run['cost']:.4f}")
m[4].metric("상태", run["status"])

if run["truncated"]:
    st.error(f"⚠ 수집 상한 도달: **{run['truncated']}** — 못 가져온 기사가 있습니다. "
             "검색 기간을 줄이거나 키워드를 더 구체적으로 나누세요.")
if run["failed"]:
    st.error(f"⚠ 수집 실패: **{run['failed']}**")
if run["status"] == "limit":
    st.warning("상한 도달로 일부가 미판정 상태입니다.")
    if st.button("이어서 판정"):
        with st.spinner("이어서 판정 중…"):
            agent.resume(conn, run["id"])
        st.rerun()

# --- 이슈 목록 ---------------------------------------------------------------
order = {"최우선": 0, "필수": 1, "참고": 2}
threads = [t for t in db.recent_threads(conn, cur["id"], days=3650)
           if t["run_id"] == run["id"]]
threads.sort(key=lambda t: (order.get(t["importance"], 9), t["title"]))

if not threads:
    st.info("이 실행에서 묶인 이슈가 없습니다.")
    st.stop()

st.markdown("### 이슈 검토")
st.caption("에이전트가 매긴 값입니다. 틀린 것만 고치고 [확정]을 누르세요. "
           "고친 내역은 기록되어 다음 개선의 근거가 됩니다.")

df = pd.DataFrame([{
    "id": t["id"],
    "중요도": t["importance"],
    "라벨": t["label"],
    "이슈": t["title"],
    "건수": t["item_count"],
    "경과": t["first_seen"],
    "근거": t["reason"] or ("미판정" if t["status"] == "held" else ""),
} for t in threads])

edited = st.data_editor(
    df, hide_index=True, width="stretch", key="editor",
    column_config={
        "id": None,
        "중요도": st.column_config.SelectboxColumn(options=GRADES, width="small"),
        "라벨": st.column_config.SelectboxColumn(options=LABELS, width="small"),
        "이슈": st.column_config.TextColumn(width="large", disabled=True),
        "건수": st.column_config.NumberColumn(width="small", disabled=True),
        "경과": st.column_config.TextColumn(width="small", disabled=True),
        "근거": st.column_config.TextColumn(width="large"),
    },
)

act = st.columns([1, 1, 4])
if act[0].button("확정", type="primary", width="stretch"):
    changed = 0
    for _, row in edited.iterrows():
        before = next(t for t in threads if t["id"] == row["id"])
        db.confirm_thread(conn, int(row["id"]), row["라벨"], row["중요도"], row["근거"])
        if (before["label"], before["importance"]) != (row["라벨"], row["중요도"]):
            changed += 1
    st.success(f"{len(edited)}건 확정 (수정 {changed}건)")

if act[1].button("엑셀 내보내기", width="stretch"):
    try:
        res = tools.export_excel(conn, run["id"])
        st.success(f"{res['row_count']}행 → `{res['file_path']}`")
    except Exception as e:
        st.error(f"엑셀 출력 실패: {e}")

# --- 기사 전건: 접혀 있을 뿐 사라지지 않는다 ---------------------------------
st.markdown("### 기사 보기")
for t in threads:
    head = (f"{MARK.get(t['importance'], ' ')} [{t['label']}] {t['title']}  "
            f"· {t['item_count']}건")
    with st.expander(head):
        if t["reason"]:
            st.caption(f"판단 근거: {t['reason']}")
        rows = db.thread_items(conn, t["id"])
        st.dataframe(
            pd.DataFrame([{
                "게시일시": r["published_at"], "제목": r["title"],
                "언론사": r["press"], "요약": r["description"], "링크": r["link"],
            } for r in rows]),
            hide_index=True, width="stretch",
            column_config={"링크": st.column_config.LinkColumn(display_text="열기")},
        )
