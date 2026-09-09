"""③ 키워드 세트 — 세트·키워드·중요도 기준 관리 + 수정률 지표 (PRD §5.5, §6.3).

기준은 전역이 아니라 세트마다 따로 갖는다. 전혀 다른 주제의 세트끼리
이력도 기준도 섞이지 않는다.
"""
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import db  # noqa: E402
from main import DEFAULT_CRITERIA  # noqa: E402

st.title("🗂 키워드 세트")


@st.cache_resource
def get_conn():
    c = db.connect()
    db.init_db(c)
    return c


conn = get_conn()
sets = db.list_sets(conn)

tab_edit, tab_new = st.tabs(["편집", "새 세트"])

with tab_edit:
    if not sets:
        st.info("세트가 없습니다. '새 세트' 탭에서 만드세요.")
    else:
        name = st.selectbox("세트", [s["name"] for s in sets])
        cur = db.get_set(conn, name)

        # --- 수정률: 재검토 시점을 사람이 기억할 필요 없이 숫자가 알려준다
        stat = db.revision_rate(conn, cur["id"], days=30)
        c = st.columns([1, 1, 3])
        c[0].metric("확정 이슈 (30일)", stat["total"])
        c[1].metric("수정률", f"{stat['rate'] * 100:.0f}%")
        with c[2]:
            st.write("")
            if stat["total"] < 5:
                st.caption("표본이 적습니다. 며칠 더 운영한 뒤 판단하세요.")
            elif stat["rate"] >= 0.20:
                st.warning("수정률이 높습니다. 중요도 기준이나 프롬프트를 점검하세요.")
            else:
                st.success("에이전트 판단이 대체로 일치합니다.")

        st.divider()
        desc = st.text_area(
            "주제 설명", cur["description"], height=80,
            help="에이전트에게 전달되는 맥락입니다. 작은 모델일수록 이 한 줄이 판단 품질을 좌우합니다.")
        kws = st.text_area("키워드 (쉼표 구분)", ", ".join(cur["keywords"]), height=80)
        orgs = st.text_input(
            "A등급 감지 조직명 (쉼표 구분, 비우면 A등급 비활성)", ", ".join(cur["org_names"]),
            help="검색어가 아니라 감지 조건입니다. 다른 키워드로 걸린 기사에 이 이름이 "
                 "나오면 최우선으로 올립니다.")
        crit = st.text_area("중요도 기준", cur["criteria"], height=260)

        if st.button("저장", type="primary"):
            conn.execute(
                "UPDATE keyword_set SET description=?, keywords=?, criteria=?, org_names=?"
                " WHERE id=?",
                (desc.strip(),
                 ",".join(k.strip() for k in kws.split(",") if k.strip()),
                 crit.strip(),
                 ",".join(o.strip() for o in orgs.split(",") if o.strip()),
                 cur["id"]))
            conn.commit()
            st.success("저장했습니다.")
            st.rerun()

        long_kws = [k for k in cur["keywords"] if len(k) >= 8]
        if long_kws:
            st.info(
                "**분절 위험 키워드**: " + ", ".join(f"`{k}`" for k in long_kws) +
                "\n\n긴 복합명사는 네이버가 형태소 단위로 `<b>` 태그를 씌워 반환할 수 있습니다. "
                "수집기가 태그를 먼저 제거하고 검사하므로 현재는 걸러지지 않지만, "
                "수집 건수가 유독 적으면 이 키워드부터 확인하세요.")

with tab_new:
    st.caption("전혀 다른 주제의 세트를 만들어도 이력과 기준이 섞이지 않습니다.")
    n_name = st.text_input("세트 이름")
    n_desc = st.text_area("주제 설명", height=80)
    n_kws = st.text_area("키워드 (쉼표 구분)", height=80)
    n_crit = st.text_area("중요도 기준", DEFAULT_CRITERIA, height=260,
                          help="기본 템플릿입니다. 이 세트에 맞게 고치세요.")
    if st.button("만들기", type="primary", disabled=not (n_name and n_kws)):
        db.get_or_create_set(
            conn, n_name.strip(),
            [k.strip() for k in n_kws.split(",") if k.strip()],
            description=n_desc.strip(), criteria=n_crit.strip())
        st.success(f"'{n_name}' 세트를 만들었습니다.")
        st.rerun()
