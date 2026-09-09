"""수집기 검증. 이 프로젝트의 출발점인 주장 — '기존 방식에 이미 누락이 있다' — 을 고정한다."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import collect  # noqa: E402

NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=collect.KST)


def raw_item(idx, title, desc, hours_ago=1, origin=None, link=None):
    pub = NOW - timedelta(hours=hours_ago)
    return {
        "title": title,
        "description": desc,
        "link": link or f"https://n.news.naver.com/article/{idx}",
        "originallink": origin if origin is not None else f"https://yna.co.kr/{idx}",
        "pubDate": pub.strftime("%a, %d %b %Y %H:%M:%S +0900"),
    }


def make_fetch(by_keyword):
    """네이버 응답을 흉내낸다. start/display 페이징을 그대로 반영한다."""
    def fetch(kw, start, display):
        items = by_keyword.get(kw, [])
        return {"items": items[start - 1: start - 1 + display]}
    return fetch


# --- 결함 2·3: 태그 분절과 엔티티 -------------------------------------------

def test_fragmented_keyword_is_the_bug():
    """기존 코드가 왜 기사를 버렸는지를 그대로 고정한다.

    네이버는 검색어를 형태소 단위로 <b> 감싸 반환할 수 있다.
    태그 제거 전에 검사하면 긴 복합명사가 매칭에 실패한다.
    """
    raw = "<b>한국</b><b>사회적기업</b><b>진흥원</b>이 발표한 자료에 따르면"
    assert "한국사회적기업진흥원" not in raw            # ← 기존 코드가 폐기하던 조건
    assert "한국사회적기업진흥원" in collect.clean(raw)  # ← 수정 후에는 걸린다


def test_html_entities_are_unescaped():
    raw = "&quot;<b>사회연대경제</b>&quot; 논의가 &amp; 확산"
    assert collect.clean(raw) == '"사회연대경제" 논의가 & 확산'


def test_fragmented_keyword_survives_collection():
    """분절된 기관명 기사가 실제로 수집 결과에 남는지 end-to-end로 확인한다."""
    kw = "한국사회적기업진흥원"
    items = [raw_item(1, "<b>한국</b><b>사회적기업</b><b>진흥원</b> 공고",
                      "내년도 사업 공고를 게시했다")]
    r = collect.search_news([kw], hours=24, fetch=make_fetch({kw: items}), now=NOW)
    assert len(r["items"]) == 1
    assert r["items"][0]["title"] == "한국사회적기업진흥원 공고"
    assert r["dropped_by_recheck"][kw] == 0


def test_unrelated_article_is_still_dropped():
    """재확인 자체는 살아 있어야 한다. 형태소 검색의 헐거운 매칭을 걸러내는 장치다."""
    kw = "마을기업"
    items = [raw_item(1, "동네 카페 창업 이야기", "마을 주민들이 모여 카페를 열었다")]
    r = collect.search_news([kw], hours=24, fetch=make_fetch({kw: items}), now=NOW)
    assert r["items"] == []
    assert r["dropped_by_recheck"][kw] == 1


def test_strict_recheck_off_keeps_everything():
    """§8.3(가) 비교 실험용 스위치."""
    kw = "마을기업"
    items = [raw_item(1, "동네 카페 창업", "마을 주민들이 카페를 열었다")]
    r = collect.search_news([kw], hours=24, strict_recheck=False,
                            fetch=make_fetch({kw: items}), now=NOW)
    assert len(r["items"]) == 1


# --- 결함 1: 페이징과 상한 ---------------------------------------------------

def test_paging_collects_beyond_first_page():
    """기존 코드는 display=100 한 번만 불러 101번째부터 조용히 잃었다."""
    kw = "사회적기업"
    items = [raw_item(i, f"사회적기업 소식 {i}", "본문") for i in range(250)]
    r = collect.search_news([kw], hours=24, max_per_kw=300,
                            fetch=make_fetch({kw: items}), now=NOW)
    assert len(r["items"]) == 250
    assert r["truncated_keywords"] == []


def test_truncation_is_reported_not_silent():
    """상한에 닿으면 중단하지 않고 '못 가져온 게 있다'고 알린다."""
    kw = "사회적기업"
    items = [raw_item(i, f"사회적기업 소식 {i}", "본문") for i in range(500)]
    r = collect.search_news([kw], hours=24, max_per_kw=200,
                            fetch=make_fetch({kw: items}), now=NOW)
    assert len(r["items"]) == 200
    assert r["truncated_keywords"] == [kw]


# --- 시간창 · 날짜 · 중복 ----------------------------------------------------

def test_pubdate_parsing_is_locale_independent():
    """strptime('%a, %d %b %Y')는 한글 Windows에서 깨진다."""
    dt = collect.parse_pubdate("Mon, 09 Sep 2026 08:12:00 +0900")
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 9, 9, 8)


def test_articles_outside_window_are_excluded():
    kw = "소셜벤처"
    items = [raw_item(1, "소셜벤처 투자", "본문", hours_ago=2),
             raw_item(2, "소셜벤처 옛 기사", "본문", hours_ago=30)]
    r = collect.search_news([kw], hours=24, fetch=make_fetch({kw: items}), now=NOW)
    assert [i["title"] for i in r["items"]] == ["소셜벤처 투자"]


def test_same_article_two_urls_is_one_row():
    """기존 코드는 link만 비교해 네이버 URL과 원문 URL을 따로 저장했다."""
    a = collect.dedup_key("https://YNA.co.kr/article/1/", "https://n.news.naver.com/9")
    b = collect.dedup_key("https://yna.co.kr/article/1", "https://n.news.naver.com/9")
    assert a == b


def test_same_article_from_two_keywords_merges():
    """키워드 두 개에 걸린 기사는 한 건으로 합쳐지고 키워드가 둘 다 남는다."""
    item = raw_item(1, "사회적기업 마을기업 공동 협약", "두 유형이 협약을 맺었다")
    r = collect.search_news(
        ["사회적기업", "마을기업"], hours=24,
        fetch=make_fetch({"사회적기업": [item], "마을기업": [item]}), now=NOW)
    assert len(r["items"]) == 1
    assert set(r["items"][0]["matched_keywords"]) == {"사회적기업", "마을기업"}


# --- 실패 격리 ---------------------------------------------------------------

def test_one_keyword_failure_does_not_stop_the_rest(monkeypatch):
    """키워드 하나가 죽어도 나머지는 계속 수집된다 (PRD §4.3)."""
    monkeypatch.setattr(collect.time, "sleep", lambda _: None)
    good = raw_item(1, "마을기업 지원 확대", "본문")

    def fetch(kw, start, display):
        if kw == "임팩트얼라이언스":
            raise ConnectionError("boom")
        return {"items": [good] if start == 1 else []}

    r = collect.search_news(["임팩트얼라이언스", "마을기업"], hours=24,
                            fetch=fetch, now=NOW)
    assert r["failed_keywords"] == ["임팩트얼라이언스"]
    assert len(r["items"]) == 1
