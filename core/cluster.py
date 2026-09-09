"""규칙 클러스터링과 라벨 판정. LLM을 부르기 전에 공짜로 끝나는 일들.

두 가지를 한다.

  1. 같은 사건 기사 묶기   — 연합뉴스를 여러 매체가 받아쓰면 제목이 거의 같다
  2. 신규/후속/기존 판정   — 같은 비교를 '새 묶음 × 기존 스레드'에 한 번 더 돌린다 (PRD §3.5)

2번을 LLM에 맡겼을 때 정확도가 5건 중 1건이었다(§9.7). 비교 연산이지 판단이 아니다.
"""
import re

# 제목 앞뒤에 붙는 장식. [속보] 【단독】 <종합> (1보) 같은 것들.
_BRACKET = re.compile(r"[\[\(【〈<][^\]\)】〉>]{0,12}[\]\)】〉>]")
_NONWORD = re.compile(r"[^0-9a-zA-Z가-힣]+")

# 실데이터 109건으로 측정해 정한 값 (§8.3 라).
#   같은 사건끼리   0.27 ~ 0.75
#   무관한 기사끼리 0.00 ~ 0.08
# 여유가 크므로 0.5로 잡는다. 과대 병합은 이슈를 가려 제1원칙을 깨므로
# 과소 병합보다 위험하지만, 이 간격에서는 둘 다 나지 않았다.
DEFAULT_THRESHOLD = 0.5

# 짧은 제목이 우연히 2개 정도 겹쳐 합쳐지는 것을 막는다.
MIN_SHARED = 3


def normalize_title(title):
    """장식과 구두점, 공백을 걷어낸다. 비교는 이 결과로만 한다."""
    t = _BRACKET.sub(" ", title or "")
    return _NONWORD.sub("", t).lower()


def _bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a, b):
    """문자 bigram 중첩계수(overlap coefficient) — 겹친 개수 / 짧은 쪽 크기.

    Jaccard를 쓰면 길이 차이에 크게 벌점이 붙는다. 실제 기사 제목은
    같은 사건이라도 길이가 두 배씩 차이나서 Jaccard가 0.12까지 떨어졌다.

        "안양시, '2026 안양춤축제' 개막"
        "춤·영화·놀이가 한자리에... '2026 안양춤축제' 18일 개막"
        → Jaccard 0.35 / 중첩계수 0.75

    짧은 쪽으로 나누므로 '한쪽이 다른 쪽에 거의 담기는가'를 본다.
    """
    ga, gb = _bigrams(normalize_title(a)), _bigrams(normalize_title(b))
    if not ga or not gb:
        return 1.0 if ga == gb else 0.0
    shared = len(ga & gb)
    if shared < MIN_SHARED:
        return 0.0
    return shared / min(len(ga), len(gb))


def cluster_items(items, threshold=DEFAULT_THRESHOLD):
    """items를 같은 사건끼리 묶는다. 전이적으로 병합한다 — A~B, B~C면 A·B·C가 한 묶음.

    대표 제목은 가장 최근 기사의 것.
    반환: [{"title": 대표제목, "items": [...]}]  — 최신순을 유지한다.

    대표 제목 하나와만 비교하는 그리디 방식은 실데이터에서 같은 사건을 쪼갰다.
    11번가 SOVAC 기사가 4+4+3 세 묶음으로, 박람회가 13+4 두 묶음으로 갈렸다.
    각 묶음 안은 맞는데 묶음끼리 만나지 못한 것이라, 전이적 병합이 정답이다.
    """
    # ponytail: 전 쌍 비교 O(n²). 109건에 1만 2천 회로 즉시 끝난다.
    #           수천 건으로 늘면 키워드나 날짜로 후보를 좁힌 뒤 비교한다.
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    grams = [_bigrams(normalize_title(it["title"])) for it in items]
    for i in range(n):
        for j in range(i + 1, n):
            ga, gb = grams[i], grams[j]
            if not ga or not gb:
                continue
            shared = len(ga & gb)
            if shared >= MIN_SHARED and shared / min(len(ga), len(gb)) >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    groups = {}
    for i in range(n):                      # 입력 순서를 유지해야 대표가 최신 기사가 된다
        groups.setdefault(find(i), []).append(items[i])
    return [{"title": g[0]["title"], "items": g} for g in groups.values()]


def match_thread(title, threads, threshold=DEFAULT_THRESHOLD):
    """기존 스레드 중 같은 사안을 찾는다. 가장 비슷한 하나만 돌려준다.

    threads는 db.recent_threads()의 결과. 없으면 None.
    """
    best, best_score = None, threshold
    for t in threads:
        s = similarity(title, t["title"])
        if s >= best_score:
            best, best_score = t, s
    return best


def decide_label(matched_thread, has_new_items):
    """신규 / 후속 / 기존 (PRD §3.5).

    matched_thread 없음      → 신규   처음 보는 사안
    있고 새 기사 있음        → 후속   이미 아는 사안의 새 보도
    있고 새 기사 없음        → 기존   전부 어제 이미 본 기사
    """
    if matched_thread is None:
        return "신규"
    return "후속" if has_new_items else "기존"
