"""클러스터링과 라벨 판정. 실제 기사 제목 형태로 검증한다."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import cluster  # noqa: E402

# 같은 사건을 매체별로 다르게 쓴 제목들
SAME_EVENT = [
    "사회연대경제기본법 국회 본회의 통과",
    "[속보] 사회연대경제기본법 통과",
    "사회연대경제기본법, 국회 문턱 넘었다",
    "與野 합의…사회연대경제기본법 국회 통과",
    "<종합> 사회연대경제기본법 본회의 의결",
]
OTHER_EVENTS = [
    "한국사회적기업진흥원 2026년 사업공고 발표",
    "○○구 마을기업 한마당 축제 열려",
]


def _items(titles):
    return [{"title": t, "id": i} for i, t in enumerate(titles)]


def test_decoration_is_stripped():
    assert cluster.normalize_title("[속보] 사회연대경제기본법 통과") == "사회연대경제기본법통과"
    assert cluster.normalize_title("<종합> 사회연대경제기본법 통과") == "사회연대경제기본법통과"


def test_same_event_titles_are_similar():
    base = SAME_EVENT[0]
    for other in SAME_EVENT[1:]:
        assert cluster.similarity(base, other) >= cluster.DEFAULT_THRESHOLD, other


def test_different_events_are_not_similar():
    for other in OTHER_EVENTS:
        assert cluster.similarity(SAME_EVENT[0], other) < cluster.DEFAULT_THRESHOLD, other


def test_five_articles_collapse_into_one_cluster():
    """연합뉴스를 여러 매체가 받아쓴 경우. 이게 100줄을 20줄로 접는 힘의 대부분이다."""
    clusters = cluster.cluster_items(_items(SAME_EVENT))
    assert len(clusters) == 1
    assert len(clusters[0]["items"]) == 5


def test_unrelated_articles_stay_separate():
    clusters = cluster.cluster_items(_items(SAME_EVENT + OTHER_EVENTS))
    assert len(clusters) == 3


def test_nothing_is_discarded_by_clustering():
    """제1원칙: 묶기만 하고 지우지 않는다."""
    titles = SAME_EVENT + OTHER_EVENTS
    clusters = cluster.cluster_items(_items(titles))
    kept = [it["title"] for c in clusters for it in c["items"]]
    assert sorted(kept) == sorted(titles)


# --- 라벨 판정 ---------------------------------------------------------------

THREADS = [
    {"id": 3, "title": "사회연대경제기본법 국회 심사 착수"},
    {"id": 7, "title": "소셜벤처 투자 위축 우려"},
]


def test_new_issue_has_no_matching_thread():
    m = cluster.match_thread("한국사회적기업진흥원 2026년 사업공고 발표", THREADS)
    assert m is None
    assert cluster.decide_label(m, has_new_items=True) == "신규"


def test_followup_matches_existing_thread():
    m = cluster.match_thread("사회연대경제기본법 국회 심사 통과", THREADS)
    assert m is not None and m["id"] == 3
    assert cluster.decide_label(m, has_new_items=True) == "후속"


def test_no_new_articles_means_unchanged():
    m = cluster.match_thread("사회연대경제기본법 국회 심사 착수", THREADS)
    assert cluster.decide_label(m, has_new_items=False) == "기존"


def test_match_picks_the_closest_thread():
    threads = [
        {"id": 1, "title": "사회연대경제기본법 국회 심사"},
        {"id": 2, "title": "사회연대경제기본법 국회 본회의 통과"},
    ]
    m = cluster.match_thread("사회연대경제기본법 본회의 통과", threads)
    assert m["id"] == 2
