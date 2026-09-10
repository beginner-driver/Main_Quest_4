"""LLM 계층. Ollama 없이 도는 단위 테스트 + 실제 모델이 있을 때만 도는 통합 테스트."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import llm  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {"importance": {"type": "string", "enum": ["최우선", "필수", "참고"]}},
    "required": ["importance"],
}


def fake_post(reply, tin=100, tout=20):
    def post(url, payload, timeout):
        post.payload = payload
        return {"message": {"content": reply},
                "prompt_eval_count": tin, "eval_count": tout}
    return post


def test_thinking_is_always_disabled():
    """켜지면 569초가 걸리고 content가 빈 문자열로 온다 (PRD §9.7). 회귀 방지."""
    post = fake_post(json.dumps({"importance": "최우선"}))
    llm.judge("sys", "user", SCHEMA, post=post)
    assert post.payload["think"] is False
    assert post.payload["options"]["temperature"] == 0
    assert post.payload["format"] == SCHEMA


def test_usage_is_recorded():
    post = fake_post(json.dumps({"importance": "필수"}), tin=498, tout=371)
    parsed, usage = llm.judge("sys", "user", SCHEMA, post=post)
    assert parsed == {"importance": "필수"}
    assert usage["in"] == 498 and usage["out"] == 371
    assert usage["cost"] == 0.0            # 로컬은 0원
    assert usage["attempts"] == 1


def test_broken_json_retries_then_raises():
    """thinking이 켜졌을 때 실제로 나오던 상황 — content가 빈 문자열."""
    calls = []

    def post(url, payload, timeout):
        calls.append(1)
        return {"message": {"content": ""}, "prompt_eval_count": 1, "eval_count": 1}

    with pytest.raises(ValueError, match="판정 호출 실패"):
        llm.judge("sys", "user", SCHEMA, post=post)
    assert len(calls) == 3                 # 3회 시도 후 포기


def test_transient_http_error_is_retried(monkeypatch):
    """모델 콜드 스타트 구간에서 실제로 났다.

    Ollama가 /api/tags에는 응답하는데 모델 로딩(약 10초) 중이라 요청이 거절된다.
    한 번 튕겼다고 30분짜리 실행을 죽일 이유가 없다.
    """
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    calls = []

    def post(url, payload, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("model is loading")
        return {"message": {"content": json.dumps({"importance": "필수"})},
                "prompt_eval_count": 10, "eval_count": 5}

    parsed, usage = llm.judge("sys", "user", SCHEMA, post=post)
    assert parsed == {"importance": "필수"}
    assert usage["attempts"] == 2


def test_api_model_cost_is_computed():
    assert llm.cost_of("claude-haiku-4-5", 1_000_000, 0) == pytest.approx(1.0)
    assert llm.cost_of("qwen3.5:2b", 1_000_000, 1_000_000) == 0.0


@pytest.mark.skipif(not llm.available(), reason="Ollama가 실행 중이 아님")
def test_real_model_honors_schema():
    """실제 모델이 enum을 지키는지. Ollama가 떠 있을 때만 돈다."""
    parsed, usage = llm.judge(
        "중요도만 답하세요.",
        "묶음: '사회연대경제기본법 국회 통과'. 법·제도 변경은 최우선입니다.",
        SCHEMA,
    )
    assert parsed["importance"] in {"최우선", "필수", "참고"}
    assert usage["in"] > 0 and usage["out"] > 0
