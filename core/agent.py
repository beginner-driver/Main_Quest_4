"""에이전트 루프 (PRD §3.1).

골격은 코드가 고정하고, 판단 구간에서만 에이전트가 돈다.

  [고정]  수집 → 클러스터링 → 이력 대조 → 라벨 판정(규칙)
  [자율]  중요도 판정 → 정보 부족 판단 → 본문 조회 → 재판정 → 다음 행동 결정
  [고정]  결과 저장 → (사람 승인)

에이전트의 '다음 행동 결정'은 응답의 need_body 필드다. 제목과 요약만으로 등급을
못 정하겠다고 판단하면 본문을 요청하고, 받아서 다시 판정한다.
"""
import time

from . import cluster, db, llm, tools

BATCH = 3          # §9.7: 5개 배치에서 후반 붕괴가 관찰됐다

# 실측: CPU 추론에서 배치당 29초, 60묶음이면 약 10분.
# 매일 도는 배경 작업이므로 20분까지 열어둔다. 상한에 닿아도 미판정 묶음은
# 목록에 남고 --resume으로 이어서 진행할 수 있다.
LIMITS = {"iterations": 3, "fetch": 3, "seconds": 1200, "tokens": 50_000}

GRADES = ["최우선", "필수", "참고"]

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "importance": {"type": "string", "enum": GRADES},
                    "reason": {"type": "string"},
                    "need_body": {"type": "boolean"},
                    "need_body_reason": {"type": "string"},
                },
                "required": ["id", "importance", "reason", "need_body"],
            },
        }
    },
    "required": ["results"],
}

SYSTEM = """당신은 '{name}' 뉴스 모니터링 담당자입니다.
{description}

[중요도 기준]
{criteria}

위에 적힌 기준만 사용하세요. 없는 기준을 만들어 쓰지 마세요.
기사에 기관이나 기업 이름이 나온다는 사실만으로는 등급을 올리지 않습니다.
어느 기준에도 뚜렷이 해당하지 않으면 '참고'입니다.

각 묶음에 importance(최우선/필수/참고)와 reason을 매기세요.

reason은 25자 이내로 짧게 쓰세요. 등급의 근거만 적습니다.
  좋은 예: "B·법 제정" / "C·공모 공고, 마감 있음" / "지역 행사"
  나쁜 예: "지역 행사 및 홍보성 기사로 사회연대경제 생태계 동향과 직접 관련이 없음"
반드시 그 묶음의 내용으로 직접 쓰세요. 다른 묶음이나 입력 문장을 그대로 옮기지 마세요.

제목과 요약만으로 등급을 정할 수 없으면 need_body를 true로 하고
need_body_reason에 무엇을 확인해야 하는지 쓰세요.
특히 법·제도 변경인데 '무엇이 어떻게 바뀌었는지'가 요약에서 잘렸다면 본문이 필요합니다.
등급을 정할 수 있으면 need_body는 false입니다."""


def _criteria_for(set_row):
    """A등급은 감지 대상 조직명이 등록됐을 때만 프롬프트에 남긴다.

    '(등록된 경우에만)' 같은 단서를 2B 모델은 무시한다. 실제로 조직명이 비어 있는데도
    '현대차 직접 언급됨' 같은 이유로 최우선을 매겼다. 조건은 설명하는 것보다
    아예 보여주지 않는 편이 확실하다.
    """
    criteria = (set_row.get("criteria") or "기준 미설정").strip()
    orgs = set_row.get("org_names") or []
    if orgs:
        return criteria.replace("(감지 대상 조직명이 등록된 경우에만)",
                                f"— 대상: {', '.join(orgs)}")
    return "\n".join(l for l in criteria.splitlines()
                     if not l.strip().startswith("A "))


def _budget_left(state, limits):
    return (time.time() - state["t0"] < limits["seconds"]
            and state["tokens"] < limits["tokens"])


def _render(batch, bodies):
    """묶음을 배치 안에서 1,2,3으로 번호 매겨 보여준다.

    전역 번호(40,41,42)를 주면 모델이 응답에서 1,2,3으로 다시 매기는 일이 있다.
    그러면 대조가 실패해 배치 전체가 통째로 버려진다. 실제로 22배치 중 8배치가
    이렇게 날아갔다. 애초에 작은 번호만 보여주면 틀릴 여지가 없다.
    """
    out = []
    for k, c in enumerate(batch, start=1):
        lines = [f"{k}. {c['title']} (기사 {len(c['items'])}건, 라벨 {c['label']})"]
        lines.append(f"   요약: {c['items'][0]['description'][:200]}")
        if k in bodies:
            lines.append(f"   본문: {bodies[k][:1500]}")
        out.append("\n".join(lines))
    return "\n".join(out)


def _collect_verdicts(parsed, batch, verdicts):
    """응답을 배치 항목에 대응시킨다. id가 어긋나면 순서로 받는다."""
    results = parsed.get("results") or []
    for k in range(1, len(batch) + 1):
        r = next((x for x in results if x.get("id") == k), None)
        if r is None and len(results) == len(batch):
            r = results[k - 1]          # id를 못 지켰어도 개수가 맞으면 순서로 매칭
        if r:
            verdicts[k] = r
    return verdicts


def _judge_batch(conn, run_id, seq, batch, set_row, threads, state, limits, judge_fn, model):
    """한 배치를 판정한다. 필요하면 본문을 받아 다시 판정한다 — 여기가 루프다."""
    history = "\n".join(
        f"- (thread {t['id']}) {t['title']} — 최초 {t['first_seen']}, 최종 {t['last_seen']}"
        for t in threads[:15]) or "- (없음)"
    system = SYSTEM.format(name=set_row["name"],
                           description=set_row["description"] or "",
                           criteria=_criteria_for(set_row))
    bodies, verdicts = {}, {}

    for it in range(limits["iterations"]):
        user = f"[기존 이슈]\n{history}\n\n[판정할 묶음]\n{_render(batch, bodies)}"
        parsed, usage = judge_fn(system, user, JUDGE_SCHEMA, model=model)
        state["tokens"] += usage["in"] + usage["out"]
        state["cost"] += usage["cost"]
        db.log_step(conn, run_id, seq[0], "judge",
                    reason=f"묶음 {len(batch)}개 판정 (반복 {it + 1})",
                    output_summary=f"in={usage['in']} out={usage['out']}",
                    duration_ms=int(usage["seconds"] * 1000))
        seq[0] += 1

        _collect_verdicts(parsed, batch, verdicts)

        # --- 다음 행동 결정: 본문이 필요하다고 판단한 묶음이 있는가
        wants = [k for k, r in verdicts.items()
                 if r.get("need_body") and k not in bodies]
        if not wants or state["fetched"] >= limits["fetch"] or not _budget_left(state, limits):
            break

        got_any = False
        for k in wants:
            if state["fetched"] >= limits["fetch"]:
                break
            why = verdicts[k].get("need_body_reason") or "요약만으로 등급 판단 불가"
            res = tools.call(conn, run_id, seq[0], "fetch_article",
                             {"item_id": batch[k - 1]["items"][0]["_id"], "reason": why},
                             reason=why)
            seq[0] += 1
            state["fetched"] += 1
            if res and res["success"]:
                bodies[k] = res["content"]
                got_any = True
            else:
                bodies[k] = ""            # 실패해도 재요청하지 않는다
        if not got_any:
            break                          # 전부 실패 → description으로 확정하고 진행

    return verdicts


def run_agent(conn, keyword_set_id, hours=24, limits=None, model=None,
              search_fn=None, judge_fn=None):
    """한 번의 실행. run_id를 돌려준다.

    search_fn / judge_fn을 주입하면 외부 호출 없이 테스트할 수 있다.
    """
    limits = {**LIMITS, **(limits or {})}
    search_fn = search_fn or tools.search_news
    judge_fn = judge_fn or llm.judge

    set_row = db.get_set(conn, keyword_set_id)
    run_id = db.create_run(conn, keyword_set_id, hours=hours)
    state = {"t0": time.time(), "tokens": 0, "cost": 0.0, "fetched": 0}
    seq = [1]

    # --- [고정] 수집
    t0 = time.time()
    found = search_fn(set_row["keywords"], hours=hours)
    db.log_step(conn, run_id, seq[0], "search_news",
                reason="정기 수집", input_summary=f"키워드 {len(set_row['keywords'])}개 · {hours}시간",
                output_summary=tools._summarize("search_news", found),
                duration_ms=int((time.time() - t0) * 1000),
                success=not found["failed_keywords"],
                error=",".join(found["failed_keywords"]))
    seq[0] += 1

    item_ids = db.upsert_items(conn, found["items"])
    for it, iid in zip(found["items"], item_ids):
        it["_id"] = iid

    # --- [고정] 클러스터링 + 이력 대조 + 라벨 판정(규칙, §3.5)
    clusters = cluster.cluster_items(found["items"])
    threads = tools.call(conn, run_id, seq[0], "lookup_history",
                         {"keyword_set_id": keyword_set_id, "days": 14},
                         reason="신규/후속 판정을 위한 과거 이슈 조회") or []
    seq[0] += 1

    pending = []
    for n, c in enumerate(clusters, start=1):
        matched = cluster.match_thread(c["title"], threads)
        linked = set()
        if matched:
            linked = {r["id"] for r in db.thread_items(conn, matched["id"])}
        ids = [it["_id"] for it in c["items"]]
        label = cluster.decide_label(matched, has_new_items=bool(set(ids) - linked))
        newest = max(it["published_at"] for it in c["items"])
        tid = db.save_thread(
            conn, keyword_set_id, run_id, c["title"],
            first_seen=(matched["first_seen"] if matched else newest[:10]),
            last_seen=newest[:10], label=label, importance="참고",
            reason="", status="pending", item_ids=ids,
            thread_id=matched["id"] if matched else None)
        pending.append({"n": n, "tid": tid, "title": c["title"],
                        "items": c["items"], "label": label})

    # --- [자율] 판정 루프
    status, judged = "done", 0
    for i in range(0, len(pending), BATCH):
        if not _budget_left(state, limits):
            status = "limit"
            break
        batch = pending[i:i + BATCH]
        verdicts = _judge_batch(conn, run_id, seq, batch, set_row, threads,
                                state, limits, judge_fn, model)
        for k, c in enumerate(batch, start=1):
            v = verdicts.get(k)
            if not v:
                continue                    # 판정 못 받은 묶음은 pending으로 남는다
            # first_seen/last_seen은 건드리지 않는다. save_thread를 재사용하면
            # 빈 문자열로 덮어써서 이 스레드가 이력 조회에서 통째로 빠진다.
            conn.execute(
                "UPDATE thread SET importance=?, agent_importance=?, reason=?,"
                " status='judged' WHERE id=?",
                (v["importance"], v["importance"], v["reason"], c["tid"]))
            judged += 1
        conn.commit()

    # 상한 도달은 실패가 아니라 기록되는 상태다. 미판정 묶음도 목록에 남는다.
    if judged < len(pending) and status == "done":
        status = "limit"
    if status == "limit":
        conn.execute("UPDATE thread SET status='held' WHERE run_id=? AND status='pending'",
                     (run_id,))
        conn.commit()

    db.finish_run(conn, run_id, status=status,
                  item_count=len(found["items"]), thread_count=len(pending),
                  tokens_in=state["tokens"], tokens_out=0, cost=state["cost"],
                  truncated=found["truncated_keywords"], failed=found["failed_keywords"])
    return run_id


def resume(conn, run_id, limits=None, model=None, judge_fn=None):
    """중단된 실행을 이어서 판정한다. 이미 판정된 묶음은 다시 계산하지 않는다."""
    limits = {**LIMITS, **(limits or {})}
    judge_fn = judge_fn or llm.judge
    run = db.get_run(conn, run_id)
    set_row = db.get_set(conn, run["keyword_set_id"])
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM thread WHERE run_id=? AND status IN ('pending','held')"
        " ORDER BY id", (run_id,))]
    if not rows:
        return run_id

    threads = db.recent_threads(conn, run["keyword_set_id"], days=14)
    state = {"t0": time.time(), "tokens": run["tokens_in"], "cost": run["cost"], "fetched": 0}
    seq = [conn.execute("SELECT COALESCE(MAX(seq),0)+1 s FROM run_step WHERE run_id=?",
                        (run_id,)).fetchone()["s"]]

    pending = [{"n": i + 1, "tid": r["id"], "title": r["title"], "label": r["label"],
                "items": db.thread_items(conn, r["id"])} for i, r in enumerate(rows)]
    for p in pending:
        for it in p["items"]:
            it["_id"] = it["id"]

    status, judged = "done", 0
    for i in range(0, len(pending), BATCH):
        if not _budget_left(state, limits):
            status = "limit"
            break
        batch = pending[i:i + BATCH]
        verdicts = _judge_batch(conn, run_id, seq, batch, set_row, threads,
                                state, limits, judge_fn, model)
        for k, c in enumerate(batch, start=1):
            v = verdicts.get(k)
            if not v:
                continue
            conn.execute(
                "UPDATE thread SET importance=?, agent_importance=?, reason=?,"
                " status='judged' WHERE id=?",
                (v["importance"], v["importance"], v["reason"], c["tid"]))
            judged += 1
        conn.commit()

    if judged < len(pending) and status == "done":
        status = "limit"
    db.finish_run(conn, run_id, status=status,
                  tokens_in=state["tokens"], cost=state["cost"])
    return run_id
