"""이슈 레이더 — 화면 진입점.

    streamlit run app.py

화면은 로직을 갖지 않는다. 전부 core/를 호출할 뿐이다.
게시판 감시를 붙일 때는 views/에 파일 하나를 추가하고 아래 목록에 한 줄 넣으면 된다.
"""
import streamlit as st

st.set_page_config(page_title="이슈 레이더", page_icon="📡", layout="wide")

st.navigation([
    st.Page("views/1_오늘의_이슈.py", title="오늘의 이슈", icon="📡",
            url_path="today", default=True),
    st.Page("views/2_실행_로그.py", title="실행 로그", icon="🧾", url_path="log"),
    st.Page("views/3_키워드_세트.py", title="키워드 세트", icon="🗂", url_path="keywords"),
    st.Page("views/4_이력.py", title="이력", icon="🗓", url_path="history"),
]).run()
