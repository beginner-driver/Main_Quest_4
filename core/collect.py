"""네이버 뉴스 수집. 기존 스크립트의 조용한 누락 3가지를 고친 판이다 (PRD §1.2).

  결함 1  display=100 고정, start 페이징 없음      → 키워드당 100건에서 잘림
  결함 2  태그 제거 '전에' 키워드 재확인            → <b> 분절된 복합명사가 폐기됨
  결함 3  HTML 엔티티(&quot; 등) 미해제             → 같은 이유로 매칭 실패

그리고 잘렸다는 사실을 알 수 있게 truncated_keywords로 돌려준다.
기존 코드의 진짜 문제는 잘린다는 것이 아니라 잘린 걸 모른다는 것이었다.
"""
import html
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

API = "https://openapi.naver.com/v1/search/news.json"
PAGE = 100        # display 최대
CEILING = 1000    # start 최대
KST = timezone(timedelta(hours=9))

_TAG = re.compile(r"<[^>]*>")


def clean(text):
    """태그를 지운 뒤 HTML 엔티티를 푼다. 순서가 중요하다.

    네이버는 검색어를 <b>로 감싸 반환하고, 형태소 단위로 쪼개 감싸는 경우가 있다.
    이 처리를 하기 전에 키워드를 검사하면 긴 복합명사가 조용히 폐기된다.
    """
    return html.unescape(_TAG.sub("", text or "")).strip()


def parse_pubdate(s):
    """'Mon, 09 Sep 2026 08:12:00 +0900' → datetime.

    strptime('%a, %d %b %Y')는 로케일 의존이라 한글 Windows에서 깨진다.
    """
    return parsedate_to_datetime(s)


def dedup_key(origin_link, link):
    """같은 기사를 한 번만 저장하기 위한 키.

    기존 코드는 link만 비교해서 네이버뉴스 URL과 언론사 원문 URL이 따로 저장됐다.
    """
    url = (origin_link or link or "").strip()
    p = urllib.parse.urlsplit(url)
    if not p.netloc:
        return url
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), p.query, "")
    )


def press_of(origin_link, link):
    # ponytail: 네이버 API가 언론사명을 안 준다. 원문 도메인으로 대신한다.
    #           실제 이름이 필요해지면 도메인→언론사 매핑 표를 둔다.
    host = urllib.parse.urlsplit(origin_link or link or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _http_get(keyword, start, display, client_id, client_secret, timeout=10):
    r = requests.get(
        API,
        params={"query": keyword, "display": display, "start": start, "sort": "date"},
        headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _collect_one(keyword, cutoff, max_items, fetch, strict_recheck):
    """키워드 하나를 페이징하며 수집. (항목들, 상한도달여부, 재확인탈락수)를 돌려준다."""
    out, dropped, start, truncated = [], 0, 1, False
    window_open = False   # 첫 페이지가 비어도 아래 상한 판정이 돌 수 있게 미리 잡아둔다

    while start <= CEILING and len(out) < max_items:
        display = min(PAGE, max_items - len(out), CEILING - start + 1)
        data = fetch(keyword, start, display)
        raw = data.get("items") or []
        if not raw:
            break

        window_open = False   # 이 페이지에 시간창 안 기사가 있었나
        for it in raw:
            pub = parse_pubdate(it["pubDate"])
            if pub < cutoff:
                continue      # sort=date라 최신순. 아래는 전부 더 오래됐다
            window_open = True

            title = clean(it.get("title", ""))
            desc = clean(it.get("description", ""))
            # 재확인은 반드시 clean() 이후에. 여기가 결함 2·3이 있던 자리다.
            if strict_recheck and keyword not in title and keyword not in desc:
                dropped += 1
                continue

            link, origin = it.get("link", ""), it.get("originallink", "")
            out.append({
                "source": "naver_news",
                "title": title,
                "description": desc,
                "link": link,
                "origin_link": origin,
                "press": press_of(origin, link),
                "published_at": pub.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S"),
                "dedup_key": dedup_key(origin, link),
                "matched_keywords": [keyword],
            })

        if not window_open:
            break             # 페이지 전체가 시간창 밖 → 더 볼 것 없음
        if len(raw) < display:
            break             # 결과 소진
        start += display

    # 상한에 닿았는데 마지막까지 시간창 안이었다면 = 못 가져온 게 남아 있다
    if (start > CEILING or len(out) >= max_items) and window_open:
        truncated = True
    return out, truncated, dropped


def search_news(keywords, hours=24, max_per_kw=300, client_id=None, client_secret=None,
                fetch=None, strict_recheck=True, now=None):
    """키워드별 OR 검색 후 합친다 (PRD §4.2 search_news).

    fetch(keyword, start, display) -> dict 를 주입하면 API 없이 테스트할 수 있다.
    strict_recheck=False면 재확인을 건너뛴다 — §8.3(가) 비교 실험용.

    반환: items / truncated_keywords / failed_keywords / dropped_by_recheck
    """
    if fetch is None:
        def fetch(kw, start, display):
            return _http_get(kw, start, display, client_id, client_secret)

    now = now or datetime.now(KST)
    cutoff = now - timedelta(hours=hours)

    merged, truncated, failed, dropped = {}, [], [], {}

    for kw in keywords:
        try:
            items, was_truncated, n_dropped = _collect_one(
                kw, cutoff, max_per_kw, fetch, strict_recheck)
        except Exception as e:                       # 개별 키워드 실패는 격리한다
            for attempt in range(2):                 # 지수 백오프 2회 재시도
                time.sleep(2 ** attempt)
                try:
                    items, was_truncated, n_dropped = _collect_one(
                        kw, cutoff, max_per_kw, fetch, strict_recheck)
                    break
                except Exception as retry_error:
                    e = retry_error
            else:
                failed.append(kw)                    # 나머지 키워드는 계속 진행한다
                dropped[kw] = 0
                continue

        if was_truncated:
            truncated.append(kw)
        dropped[kw] = n_dropped

        for it in items:                             # 키워드 간 중복은 여기서 병합
            cur = merged.get(it["dedup_key"])
            if cur:
                if kw not in cur["matched_keywords"]:
                    cur["matched_keywords"].append(kw)
            else:
                merged[it["dedup_key"]] = it

    items = sorted(merged.values(), key=lambda x: x["published_at"], reverse=True)
    return {
        "items": items,
        "truncated_keywords": truncated,
        "failed_keywords": failed,
        "dropped_by_recheck": dropped,
    }
