"""README에 넣을 화면 캡처를 만든다.

    python -m pip install playwright        # 시스템 Chrome을 쓰므로 브라우저 다운로드 불필요
    streamlit run app.py --server.port 8604
    python docs/capture.py http://localhost:8604

런타임 의존성이 아니라 문서용 도구다. requirements.txt에는 넣지 않는다.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8604"
OUT = Path(__file__).resolve().parent / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

# (경로, 파일명, 그려졌음을 알리는 문구)
# 고정 시간 대기로는 회색 스켈레톤이 그대로 찍힌다. 데이터가 실제로 붙었을 때만
# 나타나는 문구를 기다린다. 기본 페이지는 url_path가 아니라 루트로 서비스된다.
PAGES = [
    ("", "1_오늘의_이슈", "이슈 검토"),
    ("log", "2_실행_로그", "도구 호출 추적"),
    ("keywords", "3_키워드_세트", "중요도 기준"),
    ("history", "4_이력", "스레드 타임라인"),
]

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome", headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1500})
    for path, name, ready in PAGES:
        page.goto(f"{BASE}/{path}", wait_until="networkidle")
        if page.locator("text=Page not found").count():
            raise SystemExit(f"'{path}' 경로 없음. app.py 수정 후 streamlit을 재시작했나요?")
        page.get_by_text(ready, exact=False).first.wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(2000)          # 표·차트가 마저 그려질 여유
        target = OUT / f"{name}.png"
        page.screenshot(path=str(target), full_page=True)
        print(f"{target.name}  ({target.stat().st_size // 1024}KB)")
    browser.close()
