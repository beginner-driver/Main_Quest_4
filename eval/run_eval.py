"""세팅을 바꿔가며 성능 차이를 표로 남긴다 (PRD §8.3).

    python eval/run_eval.py collect            (가) 수집 재현율 — 키 필요
    python eval/run_eval.py cluster            (라) 클러스터 임계값 스윕 — DB만 있으면 됨
    python eval/run_eval.py model --models qwen3.5:2b,qwen3:4b
    python eval/run_eval.py prompt             (다) 중요도 기준 유무

(가)를 가장 먼저 돌린다. 이 프로젝트의 출발점인 주장 — '기존 방식에 이미 누락이 있다' —
을 검증하는 실험이기 때문이다.
"""
import argparse
import copy
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import agent, cluster, collect, db, llm  # noqa: E402
from main import load_env  # noqa: E402


def table(headers, rows):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths)))
    print()


# --- (가) 수집 재현율 --------------------------------------------------------

def exp_collect(conn, set_name, hours):
    """기존 코드 / 페이징만 / 페이징+태그수정 을 키워드별로 비교한다."""
    s = db.get_set(conn, set_name)
    cid, sec = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    if not cid:
        sys.exit("NAVER_CLIENT_ID가 없습니다. .env를 먼저 만드세요.")

    print(f"\n[가] 수집 재현율 — {set_name}, 최근 {hours}시간\n")
    rows = []
    for kw in s["keywords"]:
        # 기존 코드 재현: 첫 페이지 100건만, 태그 제거 전 원본에서 키워드 재확인
        legacy = 0
        page = collect._http_get(kw, 1, 100, cid, sec)
        for it in page.get("items", []):
            pub = collect.parse_pubdate(it["pubDate"])
            if (time.time() - pub.timestamp()) / 3600 > hours:
                continue
            if kw in it["title"] or kw in it["description"]:   # ← 원본에서 검사
                legacy += 1

        paged = collect.search_news([kw], hours=hours, client_id=cid, client_secret=sec,
                                    strict_recheck=False)
        fixed = collect.search_news([kw], hours=hours, client_id=cid, client_secret=sec,
                                    strict_recheck=True)

        n_paged, n_fixed = len(paged["items"]), len(fixed["items"])
        lost = n_fixed - legacy
        rows.append([kw, legacy, n_paged, n_fixed, lost,
                     f"+{lost / legacy * 100:.0f}%" if legacy else "—",
                     "예" if kw in fixed["truncated_keywords"] else ""])
        time.sleep(0.3)

    table(["키워드", "기존코드", "페이징만", "페이징+태그수정", "회복", "증가율", "상한도달"], rows)
    tot_old = sum(r[1] for r in rows)
    tot_new = sum(r[3] for r in rows)
    print(f"합계  기존 {tot_old}건 → 수정 후 {tot_new}건 "
          f"(회복 {tot_new - tot_old}건, "
          f"{(tot_new - tot_old) / tot_old * 100:.1f}% 증가)" if tot_old else "")
    print("\n※ 긴 복합명사(기관명·법령명)에서 회복량이 크면 PRD §1.2(4)의 예측이 맞은 것이다.")


# --- (라) 클러스터 임계값 ----------------------------------------------------

def exp_cluster(conn, set_name, thresholds):
    """DB에 쌓인 기사로 임계값을 스윕한다. API 호출 없이 돈다."""
    s = db.get_set(conn, set_name)
    items = [dict(r) for r in conn.execute(
        "SELECT DISTINCT i.* FROM item i JOIN item_thread it ON it.item_id = i.id"
        " JOIN thread t ON t.id = it.thread_id WHERE t.keyword_set_id = ?"
        " ORDER BY i.published_at DESC LIMIT 500", (s["id"],))]
    if not items:
        sys.exit("DB에 기사가 없습니다. 먼저 `python main.py` 로 한 번 수집하세요.")

    print(f"\n[라] 클러스터 임계값 — {set_name}, 기사 {len(items)}건\n")
    rows = []
    for th in thresholds:
        cs = cluster.cluster_items(items, threshold=th)
        sizes = sorted((len(c["items"]) for c in cs), reverse=True)
        rows.append([th, len(cs), f"{len(items) / len(cs):.1f}",
                     sizes[0] if sizes else 0,
                     sum(1 for x in sizes if x == 1)])
    table(["임계값", "묶음 수", "평균 크기", "최대 묶음", "1건짜리"], rows)
    print("※ 임계값이 낮을수록 많이 묶인다. 서로 다른 이슈가 합쳐지지 않는 선에서")
    print("   가장 낮은 값이 좋다. 과대 병합은 이슈를 가리므로 과소 병합보다 위험하다.")


# --- (나)(다) 모델·프롬프트 --------------------------------------------------

HEAD = ["세팅", "묶음", "판정", "미판정", "소요", "토큰", "비용",
        "최우선", "필수", "참고", "근거길이"]


def _fetch_once(set_row, hours):
    """기사를 한 번만 수집한다. 모든 세팅이 같은 입력을 본다."""
    cid = os.getenv("NAVER_CLIENT_ID")
    if not cid:
        sys.exit("NAVER_CLIENT_ID가 없습니다. .env를 먼저 만드세요.")
    found = collect.search_news(set_row["keywords"], hours=hours, client_id=cid,
                                client_secret=os.getenv("NAVER_CLIENT_SECRET"))
    print(f"수집 {len(found['items'])}건 — 이 기사들로 모든 세팅을 돌린다\n")
    return found


def _run_variant(set_row, found, label, hours, model=None, drop_criteria=False,
                 seconds=3600):
    """세팅 하나를 격리된 임시 DB에서 돌린다.

    같은 DB를 재사용하면 앞 세팅이 만든 스레드를 뒤 세팅이 '과거 이력'으로 읽어
    전부 후속으로 판정한다. 비교가 성립하려면 이력이 비어 있어야 한다.
    """
    # ignore_cleanup_errors: Windows는 닫은 직후의 SQLite 파일을 잠시 잡고 있어
    # 임시 폴더 삭제가 실패한다. 결과와 무관한 정리 단계이므로 무시한다.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = db.connect(Path(tmp) / "eval.db")
        try:
            db.init_db(conn)
            sid = db.get_or_create_set(
                conn, set_row["name"], set_row["keywords"],
                description=set_row["description"],
                criteria="" if drop_criteria else set_row["criteria"],
                org_names=",".join(set_row["org_names"]))

            # run_agent가 items에 _id를 심으므로 세팅마다 복사본을 준다
            payload = copy.deepcopy(found)

            def fixed_search(keywords, hours=24, **kw):
                return payload

            t0 = time.time()
            # 실험에서는 시간 상한이 결과를 자르면 안 된다. 느린 모델이 불리해진다.
            rid = agent.run_agent(conn, sid, hours=hours, model=model,
                                  limits={"seconds": seconds},
                                  search_fn=fixed_search, judge_fn=llm.judge)
            elapsed = time.time() - t0

            run = db.get_run(conn, rid)
            threads = [dict(r) for r in conn.execute(
                "SELECT importance, status, reason FROM thread")]
            judged = [t for t in threads if t["status"] == "judged"]
            dist = {g: sum(1 for t in judged if t["importance"] == g)
                    for g in ["최우선", "필수", "참고"]}
            avg_len = (sum(len(t["reason"] or "") for t in judged) / len(judged)
                       if judged else 0)
        finally:
            conn.close()

    return [label, run["thread_count"], len(judged), len(threads) - len(judged),
            f"{elapsed:.0f}초", run["tokens_in"], f"${run['cost']:.4f}",
            dist["최우선"], dist["필수"], dist["참고"], f"{avg_len:.0f}자"]


def exp_model(conn, set_name, hours, models, seconds=3600):
    s = db.get_set(conn, set_name)
    print(f"\n[나] 모델 비교 — {set_name}, 최근 {hours}시간")
    found = _fetch_once(s, hours)
    table(HEAD, [_run_variant(s, found, m, hours, model=m, seconds=seconds)
                 for m in models])
    print("※ 모든 세팅이 같은 기사·빈 이력에서 출발한다. 모델만 다르다.")
    print("※ 누락률은 각 실행을 화면에서 검토·확정한 뒤 수정률 지표로 확인한다.")


def exp_prompt(conn, set_name, hours, model=None):
    s = db.get_set(conn, set_name)
    print(f"\n[다] 중요도 기준 유무 — {set_name}, 최근 {hours}시간")
    found = _fetch_once(s, hours)
    table(HEAD, [
        _run_variant(s, found, "기준 없음", hours, model=model, drop_criteria=True),
        _run_variant(s, found, "기준 명시", hours, model=model),
    ])
    print("※ 기준이 없으면 모델은 '일반적인 뉴스 중요도'로 판단한다.")
    print("   최우선 분포가 어떻게 달라지는지가 이 실험의 관전 포인트다.")


def main():
    ap = argparse.ArgumentParser(description="이슈 레이더 평가")
    ap.add_argument("experiment", choices=["collect", "cluster", "model", "prompt"])
    ap.add_argument("--set", default="사회연대경제")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--models", default="qwen3.5:2b")
    ap.add_argument("--thresholds", default="0.25,0.30,0.35,0.40,0.45,0.50,0.60")
    ap.add_argument("--seconds", type=int, default=3600,
                    help="세팅당 시간 상한. 실험에서는 결과가 잘리지 않게 넉넉히 준다")
    args = ap.parse_args()

    load_env()
    conn = db.connect()
    db.init_db(conn)

    if args.experiment == "collect":
        exp_collect(conn, args.set, args.hours)
    elif args.experiment == "cluster":
        exp_cluster(conn, args.set, [float(t) for t in args.thresholds.split(",")])
    elif args.experiment == "model":
        exp_model(conn, args.set, args.hours, args.models.split(","), args.seconds)
    else:
        exp_prompt(conn, args.set, args.hours,
                   model=args.models.split(",")[0] if args.models else None)


if __name__ == "__main__":
    main()
