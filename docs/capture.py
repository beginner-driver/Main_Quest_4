"""README에 넣을 화면 캡처를 만든다.

    python -m pip install playwright        # 시스템 Chrome을 쓰므로 브라우저 다운로드 불필요
    streamlit run app.py --server.port 8602
    python docs/capture.py

런타임 의존성이 아니라 문서용 도구다. requirements.txt에는 넣지 않는다.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8602"
OUT = Path(__file__).resolve().parent / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

PAGES = [
    ("", "1_오늘의_이슈"),        # 기본 페이지는 url_path가 아니라 루트로 서비스된다
    ("log", "2_실행_로그"),
    ("keywords", "3_키워드_세트"),
    ("history", "4_이력"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1500})
    for path, name in PAGES:
        page.goto(f"{BASE}/{path}", wait_until="networkidle")
        # Streamlit은 웹소켓으로 그린다. 스켈레톤이 사라질 때까지 기다린다.
        page.wait_for_selector("[data-testid='stAppViewContainer']", timeout=30000)
        page.wait_for_timeout(4000)
        if page.locator("text=Page not found").count():
            raise SystemExit(f"'{path}' 경로를 찾지 못했습니다. "
                             "app.py를 고친 뒤 streamlit을 재시작했는지 확인하세요.")
        target = OUT / f"{name}.png"
        page.screenshot(path=str(target), full_page=True)
        print(f"{target.name}  ({target.stat().st_size // 1024}KB)")
    browser.close()
