"""이슈 레이더 CLI.

    python main.py --init                       기본 키워드 세트 생성
    python main.py --set 사회연대경제 --hours 24   수집 + 판정
    python main.py --resume 3                   중단된 실행 이어서
    python main.py --list                       세트와 최근 실행

작업 스케줄러나 n8n이 붙는 지점이 여기다. 화면 없이 완결되는 파이프라인이며,
이 인터페이스만 있으면 자동 실행을 붙이기 위해 따로 준비할 것이 없다.
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import agent, db  # noqa: E402

DEFAULT_SET = "사회연대경제"
DEFAULT_KEYWORDS = [
    "사회연대경제", "사회적기업", "마을기업", "소셜벤처",
    "임팩트얼라이언스", "한국사회적기업진흥원", "사회연대경제기본법",
]
DEFAULT_DESCRIPTION = (
    "사회연대경제 생태계 동향을 감시한다. 자기 조직 평판이 아니라 "
    "제도 변화와 유관 기관의 움직임이 관심사다."
)
DEFAULT_CRITERIA = """최우선
  A 지정 조직·사업이 직접 언급됨 (감지 대상 조직명이 등록된 경우에만)
  B 법·제도가 실제로 바뀐 기사 — 법률·조례·시행령·지침이 제정·개정·통과된 사실 보도.
    근거에 반드시 그 법령 이름을 적을 것. 법령 이름을 못 적으면 B가 아니다.
    B가 아닌 것: 정책 방향 논의 / 조직 개편 / 업무협약·MOU / 의원·단체장 발언 /
                 기고·칼럼·인터뷰 / 법을 배경으로 언급만 한 기사
필수
  C 예산·공모·지원사업 — 공고가 실제로 났거나 예산 규모가 확정·증감된 기사
  D 유사·경쟁 기관 동향.  트리거: 한국사회적기업진흥원, 임팩트얼라이언스
  E 부정 이슈 — 사고, 논란, 감사, 비판
  F 통계·현황·실태조사 결과 발표
참고
  그 외 — 지역 행사, 홍보성 기사, 기업 사회공헌, 기획·미담 기사, 단순 소개

애매하면 참고다. 등급을 올리려면 그 근거가 기사에 명시되어 있어야 한다.
기사에 없는 사실을 근거로 지어내지 말 것."""


def load_env(path=None):
    """.env를 읽는다. 의존성 하나 아끼려고 직접 판다."""
    p = Path(path or ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def cmd_init(conn):
    sid = db.get_or_create_set(conn, DEFAULT_SET, DEFAULT_KEYWORDS,
                               description=DEFAULT_DESCRIPTION,
                               criteria=DEFAULT_CRITERIA)
    print(f"세트 '{DEFAULT_SET}' 준비됨 (id={sid})")
    print(f"  키워드 {len(DEFAULT_KEYWORDS)}개: {', '.join(DEFAULT_KEYWORDS)}")


def cmd_list(conn):
    for s in db.list_sets(conn):
        run = db.latest_run(conn, s["id"])
        tail = (f"최근 실행 #{run['id']} {run['started_at']} [{run['status']}] "
                f"{run['item_count']}건 → {run['thread_count']}묶음") if run else "실행 이력 없음"
        print(f"[{s['id']}] {s['name']}\n     {s['keywords']}\n     {tail}")


def cmd_run(conn, name, hours):
    s = db.get_set(conn, name)
    if not s:
        sys.exit(f"세트 '{name}' 없음. --init 또는 --list로 확인하세요.")
    if not os.getenv("NAVER_CLIENT_ID"):
        sys.exit("NAVER_CLIENT_ID가 없습니다. .env.example을 복사해 .env를 만드세요.")

    print(f"[{s['name']}] 최근 {hours}시간 · 키워드 {len(s['keywords'])}개")
    run_id = agent.run_agent(conn, s["id"], hours=hours)
    report(conn, run_id)


def cmd_resume(conn, run_id):
    run = db.get_run(conn, run_id)
    if not run:
        sys.exit(f"실행 #{run_id} 없음")
    print(f"실행 #{run_id} 이어서 진행합니다 (이전 상태: {run['status']})")
    agent.resume(conn, run_id)
    report(conn, run_id)


def report(conn, run_id):
    run = db.get_run(conn, run_id)
    print(f"\n실행 #{run_id} [{run['status']}]  "
          f"{run['item_count']}건 → {run['thread_count']}묶음  "
          f"토큰 {run['tokens_in']}  {run['cost']:.4f} USD")

    if run["truncated"]:
        print(f"  ⚠ 수집 상한 도달: {run['truncated']} — 못 가져온 기사가 있습니다")
    if run["failed"]:
        print(f"  ⚠ 수집 실패: {run['failed']}")
    if run["status"] == "limit":
        print(f"  ⚠ 상한 도달로 일부 미판정. `--resume {run_id}` 로 이어서 진행하세요")

    order = {"최우선": 0, "필수": 1, "참고": 2}
    threads = [t for t in db.recent_threads(conn, run["keyword_set_id"], days=3650)
               if t["run_id"] == run_id]
    threads.sort(key=lambda t: (order.get(t["importance"], 9), t["title"]))
    print()
    for t in threads:
        mark = {"최우선": "★", "필수": "·", "참고": " "}.get(t["importance"], " ")
        held = " (미판정)" if t["status"] == "held" else ""
        print(f" {mark} [{t['label']}] {t['title']}  ({t['item_count']}건){held}")
        if t["reason"]:
            print(f"     └ {t['reason']}")


def main():
    ap = argparse.ArgumentParser(description="이슈 레이더")
    ap.add_argument("--init", action="store_true", help="기본 키워드 세트 생성")
    ap.add_argument("--list", action="store_true", help="세트와 최근 실행 보기")
    ap.add_argument("--set", default=DEFAULT_SET, help="키워드 세트 이름")
    ap.add_argument("--hours", type=int, default=24, help="최근 몇 시간 (첫 실행은 48 권장)")
    ap.add_argument("--resume", type=int, metavar="RUN_ID", help="중단된 실행 이어서")
    args = ap.parse_args()

    load_env()
    conn = db.connect()
    db.init_db(conn)

    if args.init:
        cmd_init(conn)
    elif args.list:
        cmd_list(conn)
    elif args.resume:
        cmd_resume(conn, args.resume)
    else:
        cmd_run(conn, args.set, args.hours)


if __name__ == "__main__":
    main()
