"""에이전트 루프. 종료 조건과 재개, 그리고 '다음 행동 결정'이 실제로 도는지."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import agent, db, tools  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def set_id(conn):
    return db.get_or_create_set(
        conn, "사회연대경제",
        ["사회연대경제기본법", "한국사회적기업진흥원", "마을기업"],
        description="사회연대경제 생태계 동향을 감시한다.",
        criteria="최우선: 법·제도 변경 / 필수: 예산·공모, 유관기관 / 참고: 그 외")


# 서로 확실히 다른 사안들. "마을기업 1번/2번" 식으로 번호만 바꾸면
# 제목 유사도가 높아 한 묶음으로 합쳐지므로 배치 테스트가 성립하지 않는다.
DISTINCT = [
    "사회연대경제기본법 국회 통과",
    "한국사회적기업진흥원 사업공고 발표",
    "마을기업 매출 증가세 뚜렷",
    "소셜벤처 투자 혹한기 지속",
    "임팩트얼라이언스 신임 대표 선임",
    "협동조합 설립 요건 완화 검토",
    "자활기업 지원 예산 삭감 논란",
    "청년 창업 지원센터 문 열어",
    "사회적경제 박람회 부산서 개최",
    "돌봄 서비스 인력난 심화",
    "지역화폐 발행 규모 축소",
    "공공기관 우선구매 실적 공개",
]


def distinct_articles(n):
    return [article(i + 1, DISTINCT[i]) for i in range(n)]


def article(i, title, desc="요약 내용", day="09"):
    return {"source": "naver_news", "title": title, "description": desc,
            "link": f"https://n.news.naver.com/article/{i}",
            "origin_link": f"https://yna.co.kr/{i}", "press": "yna.co.kr",
            "published_at": f"2026-09-{day} 08:00:00", "dedup_key": f"key{i}",
            "matched_keywords": ["사회연대경제기본법"]}


def fake_search(items, truncated=(), failed=()):
    def search(keywords, hours=24, **kw):
        return {"items": items, "truncated_keywords": list(truncated),
                "failed_keywords": list(failed), "dropped_by_recheck": {}}
    return search


GRADE_CODE = {"최우선": ("B", "사회연대경제기본법"), "필수": ("C", ""), "참고": ("none", "")}


def fake_judge(grade="필수", need_body=False, calls=None):
    """모델은 code/law만 돌려준다. 등급은 agent.grade_of()가 매긴다."""
    code, law = GRADE_CODE[grade]

    def judge(system, user, schema, model=None, **kw):
        if calls is not None:
            calls.append(user)
        ids = [int(line.split(".")[0]) for line in user.splitlines()
               if line and line[0].isdigit()]
        want = need_body and not any("본문:" in l for l in user.splitlines())
        titles = [l.split(". ", 1)[1].split(" (기사")[0] for l in user.splitlines()
                  if l and l[0].isdigit() and ". " in l]
        return ({"results": [{"id": i, "title_head": titles[i - 1][:10],
                              "code": code, "law": law,
                              "reason": f"{i}번 근거", "need_body": want,
                              "need_body_reason": "개정 내용이 요약에서 잘림"}
                             for i in ids]},
                {"in": 400, "out": 100, "seconds": 1.0, "model": "fake",
                 "cost": 0.0, "attempts": 1})
    return judge


# --- 기본 흐름 ---------------------------------------------------------------

def test_full_run_clusters_labels_and_judges(conn, set_id):
    items = [
        article(1, "사회연대경제기본법 국회 본회의 통과"),
        article(2, "[속보] 사회연대경제기본법 통과"),
        article(3, "한국사회적기업진흥원 2026년 사업공고 발표"),
    ]
    rid = agent.run_agent(conn, set_id, search_fn=fake_search(items),
                          judge_fn=fake_judge("최우선"))
    run = db.get_run(conn, rid)
    assert run["status"] == "done"
    assert run["item_count"] == 3
    assert run["thread_count"] == 2          # 1·2번이 한 묶음
    threads = db.recent_threads(conn, set_id)
    assert all(t["status"] == "judged" for t in threads)
    assert all(t["label"] == "신규" for t in threads)


def test_nothing_is_discarded(conn, set_id):
    """제1원칙. 참고 등급이어도 기사는 전부 DB에 남는다."""
    items = [article(i, f"마을기업 소식 {i}") for i in range(1, 6)]
    agent.run_agent(conn, set_id, search_fn=fake_search(items),
                    judge_fn=fake_judge("참고"))
    assert conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"] == 5


def test_followup_label_on_second_run(conn, set_id):
    """같은 사안의 새 기사는 신규가 아니라 후속이어야 한다."""
    day1 = [article(1, "사회연대경제기본법 국회 심사 착수")]
    agent.run_agent(conn, set_id, search_fn=fake_search(day1), judge_fn=fake_judge())

    day2 = [article(2, "사회연대경제기본법 국회 심사 통과", day="10")]
    rid = agent.run_agent(conn, set_id, search_fn=fake_search(day2), judge_fn=fake_judge())
    t = [x for x in db.recent_threads(conn, set_id) if x["run_id"] == rid]
    assert len(t) == 1 and t[0]["label"] == "후속"


def test_truncation_warning_reaches_the_run(conn, set_id):
    rid = agent.run_agent(conn, set_id,
                          search_fn=fake_search([article(1, "마을기업 소식")],
                                                truncated=["사회연대경제기본법"]),
                          judge_fn=fake_judge())
    assert db.get_run(conn, rid)["truncated"] == "사회연대경제기본법"


# --- 다음 행동 결정 ----------------------------------------------------------

def test_agent_requests_body_then_rejudges(conn, set_id, monkeypatch):
    """need_body=true → 본문 조회 → 본문을 담아 재판정. 이게 루프다."""
    monkeypatch.setattr(tools, "fetch_article",
                        lambda c, item_id, reason, **kw:
                        {"success": True, "content": "인증제를 등록제로 전환한다",
                         "error": ""})
    calls = []
    agent.run_agent(conn, set_id,
                    search_fn=fake_search([article(1, "사회연대경제기본법 통과")]),
                    judge_fn=fake_judge("최우선", need_body=True, calls=calls))
    assert len(calls) == 2                       # 1차 판정 → 본문 확보 → 2차 판정
    assert "본문:" in calls[1]
    steps = [s["tool"] for s in db.run_steps(conn, 1)]
    assert "fetch_article" in steps


def test_body_fetch_failure_falls_back_to_description(conn, set_id, monkeypatch):
    """본문 실패는 중단 사유가 아니다. description으로 판정하고 진행한다 (PRD §4.3)."""
    monkeypatch.setattr(tools, "fetch_article",
                        lambda c, item_id, reason, **kw:
                        {"success": False, "content": "", "error": "timeout"})
    rid = agent.run_agent(conn, set_id,
                          search_fn=fake_search([article(1, "사회연대경제기본법 통과")]),
                          judge_fn=fake_judge("최우선", need_body=True))
    assert db.get_run(conn, rid)["status"] == "done"
    assert db.recent_threads(conn, set_id)[0]["importance"] == "최우선"


def test_fetch_budget_is_capped(conn, set_id, monkeypatch):
    seen = []

    def fake_fetch(c, item_id, reason, **kw):
        seen.append(item_id)
        return {"success": True, "content": "본문", "error": ""}

    monkeypatch.setattr(tools, "fetch_article", fake_fetch)
    agent.run_agent(conn, set_id, search_fn=fake_search(distinct_articles(9)),
                    judge_fn=fake_judge("최우선", need_body=True),
                    limits={"fetch": 3})
    assert len(seen) == 3          # 9개 묶음 전부가 본문을 원해도 상한에서 멈춘다


# --- 종료 조건과 재개 --------------------------------------------------------

def _count(conn, status):
    return conn.execute("SELECT COUNT(*) c FROM thread WHERE status=?",
                        (status,)).fetchone()["c"]


def test_token_limit_holds_remaining_clusters(conn, set_id):
    """상한 도달은 실패가 아니라 기록되는 상태. 미판정 묶음도 사라지지 않는다."""
    rid = agent.run_agent(conn, set_id, search_fn=fake_search(distinct_articles(12)),
                          judge_fn=fake_judge(), limits={"tokens": 600})
    run = db.get_run(conn, rid)
    assert run["status"] == "limit"
    assert _count(conn, "held") > 0 and _count(conn, "judged") > 0
    assert _count(conn, "held") + _count(conn, "judged") == run["thread_count"]


def test_resume_only_judges_what_is_left(conn, set_id):
    rid = agent.run_agent(conn, set_id, search_fn=fake_search(distinct_articles(12)),
                          judge_fn=fake_judge(), limits={"tokens": 600})
    before = _count(conn, "judged")
    already = {r["title"] for r in conn.execute(
        "SELECT title FROM thread WHERE status='judged'")}

    calls = []
    agent.resume(conn, rid, judge_fn=fake_judge(calls=calls))

    assert _count(conn, "judged") > before
    assert _count(conn, "held") == 0 and _count(conn, "pending") == 0
    # 이미 판정된 묶음은 다시 판정하지 않는다.
    # (이력 맥락으로 프롬프트에 등장하는 것은 정상이므로 판정 구역만 본다)
    to_judge = "\n".join(c.split("[판정할 묶음]", 1)[1] for c in calls)
    assert not any(t in to_judge for t in already)


# --- 프롬프트 구성 -----------------------------------------------------------

CRIT = """최우선
  A 지정 조직·사업이 직접 언급됨 (감지 대상 조직명이 등록된 경우에만)
  B 법·제도 변경
필수
  C 예산·공모"""


def test_grade_a_is_hidden_when_no_org_registered():
    """조직명이 없으면 A등급을 프롬프트에서 아예 뺀다.

    '(등록된 경우에만)' 단서를 2B 모델이 무시하고 '현대차 직접 언급됨' 같은
    이유로 최우선을 매기는 것이 실제로 관찰됐다.
    """
    out = agent._criteria_for({"criteria": CRIT, "org_names": []})
    assert "A 지정 조직" not in out
    assert "B 법·제도 변경" in out and "C 예산·공모" in out


def test_grade_a_names_the_targets_when_registered():
    out = agent._criteria_for({"criteria": CRIT, "org_names": ["우리재단", "○○센터"]})
    assert "A 지정 조직" in out
    assert "대상: 우리재단, ○○센터" in out
    assert "등록된 경우에만" not in out


# --- 응답 대조 ---------------------------------------------------------------

def _batch(n=3):
    return [{"n": 40 + i, "title": f"묶음{i}", "items": [], "label": "신규"}
            for i in range(n)]


def test_verdicts_matched_by_batch_local_id():
    parsed = {"results": [{"id": 1, "importance": "최우선"},
                          {"id": 2, "importance": "필수"},
                          {"id": 3, "importance": "참고"}]}
    v = agent._collect_verdicts(parsed, _batch(), {})
    assert [v[k]["importance"] for k in (1, 2, 3)] == ["최우선", "필수", "참고"]


def test_verdicts_fall_back_to_order_when_ids_are_wrong():
    """모델이 id를 제멋대로 매겨도 개수가 맞으면 순서로 받는다.

    실제로 22배치 중 8배치가 id 불일치로 통째로 버려졌다.
    """
    parsed = {"results": [{"id": 40, "importance": "최우선"},
                          {"id": 41, "importance": "필수"},
                          {"id": 42, "importance": "참고"}]}
    v = agent._collect_verdicts(parsed, _batch(), {})
    assert len(v) == 3
    assert v[1]["importance"] == "최우선" and v[3]["importance"] == "참고"


def test_empty_results_leaves_batch_unjudged():
    """개수가 안 맞으면 억지로 끼워맞추지 않는다. 미판정으로 남겨 재개하게 한다."""
    assert agent._collect_verdicts({"results": []}, _batch(), {}) == {}
    assert agent._collect_verdicts({"results": [{"id": 9}]}, _batch(), {}) == {}


# --- 등급 매핑 ---------------------------------------------------------------

def test_grade_is_derived_from_code_not_chosen_by_model():
    assert agent.grade_of({"code": "B", "law": "사회연대경제기본법"})[0] == "최우선"
    assert agent.grade_of({"code": "C", "law": ""})[0] == "필수"
    assert agent.grade_of({"code": "none", "law": ""})[0] == "참고"


def test_b_law_must_appear_in_the_article():
    """모델이 댄 법령명이 기사에 없으면 강등한다.

    법령명을 필수로 만들었더니 프롬프트의 '기존 이슈' 목록에서 이름을 베껴 와
    검증을 통과했다 — 자금 지원 기사에 '사회연대경제기본법 통과'.
    """
    v = {"code": "B", "law": "사회연대경제기본법", "reason": "법 통과"}
    ok, _ = agent.grade_of(v, "국회에서 사회연대경제기본법이 통과됐다")
    assert ok == "최우선"
    bad, why = agent.grade_of(v, "새마을금고가 사회연대경제에 1.1조를 푼다")
    assert bad == "참고" and "기사에 없음" in why


def test_law_match_ignores_spacing():
    v = {"code": "B", "law": "사회연대경제 기본법"}
    assert agent.grade_of(v, "…사회연대경제기본법 시행령…")[0] == "최우선"


def test_code_a_is_rejected_when_no_org_registered():
    """A는 조직명이 등록됐을 때만 유효하다. 프롬프트에서 빼도 모델이 낼 수 있다."""
    v = {"code": "A", "law": "", "reason": "조직 언급"}
    grade, why = agent.grade_of(v, "현대차가 기증했다", a_enabled=False)
    assert grade == "참고" and "미등록" in why
    assert agent.grade_of(v, "우리재단 소식", a_enabled=True)[0] == "최우선"


def test_b_without_law_name_is_demoted():
    """법령명 없는 B 주장은 참고로 내린다.

    실측에서 최우선 9건의 근거가 전부 '법령명 없는 B·법 제정'이었다.
    프롬프트로 막히지 않아 코드로 막는다.
    """
    grade, why = agent.grade_of({"code": "B", "law": "   ", "reason": "B·법 제정"})
    assert grade == "참고"
    assert "법령명" in why


def test_unknown_code_falls_back_to_lowest():
    assert agent.grade_of({})[0] == "참고"
    assert agent.grade_of({"code": "Z"})[0] == "참고"


def test_verdicts_realign_when_model_shuffles_content():
    """모델이 id는 맞게 내면서 내용을 섞어 답하면 제목으로 바로잡는다.

    실제로 의원 발언 기사에 통합돌봄 사업 근거가 붙는 일이 있었다.
    """
    batch = [{"n": 1, "title": "전종규 동해시의원 개편 요구", "items": [], "label": "신규"},
             {"n": 2, "title": "김제시 통합돌봄 경사로 설치", "items": [], "label": "신규"},
             {"n": 3, "title": "영월군복지관 템플스테이", "items": [], "label": "신규"}]
    # id는 1,2,3인데 내용이 한 칸씩 밀려 있다
    parsed = {"results": [
        {"id": 1, "title_head": "김제시 통합돌봄 경사", "code": "D", "reason": "통합돌봄"},
        {"id": 2, "title_head": "영월군복지관 템플스테", "code": "E", "reason": "템플스테이"},
        {"id": 3, "title_head": "전종규 동해시의원 개", "code": "C", "reason": "의원 발언"},
    ]}
    v = agent._collect_verdicts(parsed, batch, {})
    assert v[1]["reason"] == "의원 발언"      # 1번 = 전종규
    assert v[2]["reason"] == "통합돌봄"       # 2번 = 김제시
    assert v[3]["reason"] == "템플스테이"     # 3번 = 영월군


def test_title_head_match_ignores_spacing():
    batch = [{"n": 1, "title": "사회연대경제 기본법 통과", "items": [], "label": "신규"}]
    parsed = {"results": [{"id": 9, "title_head": "사회연대경제기본법통", "code": "B",
                           "law": "사회연대경제기본법", "reason": "통과"}]}
    v = agent._collect_verdicts(parsed, batch, {})
    assert v[1]["reason"] == "통과"           # id가 틀려도 제목으로 찾는다


def test_judge_failure_leaves_batch_pending_not_crash(conn, set_id):
    """판정 호출이 죽어도 실행 전체가 죽지 않는다.

    Ollama가 순간적으로 흔들리면 30분짜리 실행이 통째로 날아가던 구조였다.
    이제 그 배치만 미판정으로 남고 --resume이 이어서 처리한다.
    """
    def dying_judge(system, user, schema, model=None, **kw):
        raise ConnectionError("ollama died")

    rid = agent.run_agent(conn, set_id, search_fn=fake_search(distinct_articles(6)),
                          judge_fn=dying_judge)
    run = db.get_run(conn, rid)
    assert run["status"] == "limit"                 # 완료로 위장하지 않는다
    assert _count(conn, "held") == run["thread_count"]
    failed = [s for s in db.run_steps(conn, rid) if not s["success"]]
    assert failed and "ConnectionError" in failed[0]["error"]

    # 되살아나면 이어서 마저 판정된다
    agent.resume(conn, rid, judge_fn=fake_judge())
    assert _count(conn, "held") == 0 and _count(conn, "judged") > 0
