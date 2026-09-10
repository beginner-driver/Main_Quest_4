"""판단 계층 호출. 모델을 바꾸는 지점은 이 파일의 judge() 하나다.

측정 결과(PRD §9.7) 두 가지가 여기에 못 박혀 있다.

  think=False   qwen3.5는 thinking 모델이고 Ollama에서 기본 활성이다.
                켜두면 추론 토큰을 4천 개 쏟고 content를 빈 문자열로 반환한다.
                569초 → 6.9초. 선택이 아니라 필수 설정이다.

  format=schema JSON 스키마를 넘기면 2B 모델도 enum을 정확히 지킨다.
                구조화 출력 없이는 작은 모델의 응답을 신뢰할 수 없다.
"""
import json
import os
import time

import requests

DEFAULT_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")

# 로컬 추론은 0원. API로 바꿀 때만 채운다. (입력, 출력) USD per 1M tokens
PRICING = {
    "qwen3.5:2b": (0.0, 0.0),
    "qwen3:4b": (0.0, 0.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def cost_of(model, tokens_in, tokens_out):
    pin, pout = PRICING.get(model, (0.0, 0.0))
    return tokens_in / 1e6 * pin + tokens_out / 1e6 * pout


def _post(url, payload, timeout):
    r = requests.post(f"{url}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def judge(system, user, schema, model=None, url=None, timeout=300, post=None):
    """구조화된 판단을 받는다.

    반환: (parsed_dict, usage)   usage = {"in": int, "out": int, "seconds": float,
                                          "model": str, "cost": float}
    스키마를 못 지키면 1회 재시도 후 ValueError.
    post를 주입하면 Ollama 없이 테스트할 수 있다.
    """
    model = model or DEFAULT_MODEL
    url = url or DEFAULT_URL
    post = post or _post
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": schema,
        "stream": False,
        "think": False,          # 절대 켜지 말 것. 위 주석 참조.
        "options": {"temperature": 0},
    }

    last_error = None
    for attempt in range(2):
        t0 = time.time()
        body = post(url, payload, timeout)
        elapsed = time.time() - t0
        content = (body.get("message") or {}).get("content") or ""
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as e:
            last_error = f"{e} / content={content[:200]!r}"
            continue
        tin = body.get("prompt_eval_count") or 0
        tout = body.get("eval_count") or 0
        return parsed, {
            "in": tin, "out": tout, "seconds": round(elapsed, 1),
            "model": model, "cost": cost_of(model, tin, tout),
            "attempts": attempt + 1,
        }
    raise ValueError(f"모델이 스키마를 지키지 못했습니다: {last_error}")


def available(url=None, timeout=3):
    """Ollama가 떠 있는지. 화면에서 안내 문구를 띄우는 데 쓴다."""
    try:
        r = requests.get(f"{url or DEFAULT_URL}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False
