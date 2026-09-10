"""DB 계층 검증. 핵심은 '같은 기사를 두 번 넣어도 한 행'과 세트별 격리다."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    yield c
    c.close()


def _item(key, title="제목", kws=("사회적기업",)):
    return {"title": title, "description": "요약", "link": f"https://n.news/{key}",
            "origin_link": key, "press": "연합뉴스",
            "published_at": "2026-09-09 08:12:00", "dedup_key": key,
            "matched_keywords": list(kws)}


def test_dedup_key_blocks_duplicate(conn):
    """같은 dedup_key를 두 번 넣어도 행은 하나고 같은 id가 돌아온다."""
    first = db.upsert_items(conn, [_item("A")])
    second = db.upsert_items(conn, [_item("A")])
    assert first == second
    total = conn.execute("SELECT COUNT(*) c FROM item").fetchone()["c"]
    assert total == 1


def test_matched_keywords_merge(conn):
    """다른 키워드로 다시 걸린 기사는 키워드가 병합된다 (덮어쓰지 않는다)."""
    db.upsert_items(conn, [_item("A", kws=["사회적기업"])])
    db.upsert_items(conn, [_item("A", kws=["협동조합"])])
    row = conn.execute("SELECT matched_keywords m FROM item WHERE dedup_key='A'").fetchone()
    assert set(row["m"].split(",")) == {"사회적기업", "협동조합"}


def test_get_or_create_set_is_idempotent(conn):
    a = db.get_or_create_set(conn, "사회연대경제", ["사회적기업", "마을기업"])
    b = db.get_or_create_set(conn, "사회연대경제", ["무시됨"])
    assert a == b
    s = db.get_set(conn, "사회연대경제")
    assert s["keywords"] == ["사회적기업", "마을기업"]


def test_threads_are_isolated_per_set(conn):
    """전혀 다른 주제의 세트끼리 이력이 섞이지 않아야 한다."""
    s1 = db.get_or_create_set(conn, "세트1", ["가"])
    s2 = db.get_or_create_set(conn, "세트2", ["나"])
    r1 = db.create_run(conn, s1)
    db.save_thread(conn, s1, r1, "세트1 이슈", "2026-09-09", "2026-09-09")
    assert len(db.recent_threads(conn, s1)) == 1
    assert len(db.recent_threads(conn, s2)) == 0


def test_item_belongs_to_threads_in_two_sets(conn):
    """유사한 세트에서 같은 기사가 걸려도 양쪽 모두에 노출되어야 한다.

    item.thread_id 한 칸이었다면 나중 세트가 앞 세트의 연결을 덮어써 누락이 났을 것이다.
    """
    s1 = db.get_or_create_set(conn, "세트1", ["가"])
    s2 = db.get_or_create_set(conn, "세트2", ["나"])
    (iid,) = db.upsert_items(conn, [_item("A")])
    t1 = db.save_thread(conn, s1, None, "묶음1", "2026-09-09", "2026-09-09", item_ids=[iid])
    t2 = db.save_thread(conn, s2, None, "묶음2", "2026-09-09", "2026-09-09", item_ids=[iid])
    assert [r["id"] for r in db.thread_items(conn, t1)] == [iid]
    assert [r["id"] for r in db.thread_items(conn, t2)] == [iid]


def test_revision_rate_counts_human_changes(conn):
    """사람이 고친 것만 수정으로 잡힌다. 그대로 승인한 건은 아니다."""
    s = db.get_or_create_set(conn, "세트", ["가"])
    keep = db.save_thread(conn, s, None, "그대로", "2026-09-09", "2026-09-09",
                          label="신규", importance="참고")
    fix = db.save_thread(conn, s, None, "고침", "2026-09-09", "2026-09-09",
                         label="신규", importance="참고")
    db.confirm_thread(conn, keep, "신규", "참고")
    db.confirm_thread(conn, fix, "신규", "최우선")
    stat = db.revision_rate(conn, s)
    assert stat == {"total": 2, "revised": 1, "rate": 0.5}


def test_run_and_steps_are_recorded(conn):
    s = db.get_or_create_set(conn, "세트", ["가"])
    rid = db.create_run(conn, s, hours=24)
    db.log_step(conn, rid, 1, "search_news", reason="정기 수집",
                output_summary="112건", duration_ms=8200)
    db.log_step(conn, rid, 2, "fetch_article", reason="B등급 후보인데 내용 불명",
                success=False, error="timeout")
    db.finish_run(conn, rid, status="done", item_count=112, thread_count=18,
                  tokens_in=498, tokens_out=371, truncated=["사회적기업"])
    steps = db.run_steps(conn, rid)
    assert [x["tool"] for x in steps] == ["search_news", "fetch_article"]
    assert steps[1]["success"] == 0 and steps[1]["error"] == "timeout"
    run = db.get_run(conn, rid)
    assert run["status"] == "done" and run["item_count"] == 112
    assert run["truncated"] == "사회적기업"


def test_miss_rate_counts_only_upgrades(conn):
    """누락률은 방향을 구분한다. 사람이 등급을 '올린' 것만 누락으로 센다.

    AI가 과하게 올린 것을 내리는 건 귀찮을 뿐 놓치는 게 아니다.
    """
    s = db.get_or_create_set(conn, "세트", ["가"])
    mk = lambda t, g: db.save_thread(conn, s, None, t, "2026-09-09", "2026-09-09",
                                     importance=g)
    missed = mk("놓칠 뻔", "참고")      # AI 참고 → 사람 최우선
    over = mk("과했던 것", "최우선")     # AI 최우선 → 사람 참고
    same = mk("그대로", "필수")

    db.confirm_thread(conn, missed, "신규", "최우선")
    db.confirm_thread(conn, over, "신규", "참고")
    db.confirm_thread(conn, same, "신규", "필수")

    stat = db.miss_rate(conn, s)
    assert stat["total"] == 3
    assert stat["missed"] == 1          # 올린 것만
    assert stat["over"] == 1            # 내린 것은 따로 센다
    assert stat["rate"] == pytest.approx(1 / 3)


def test_miss_rate_is_zero_when_nothing_confirmed(conn):
    s = db.get_or_create_set(conn, "빈세트", ["가"])
    assert db.miss_rate(conn, s) == {"total": 0, "missed": 0, "over": 0, "rate": 0.0}
