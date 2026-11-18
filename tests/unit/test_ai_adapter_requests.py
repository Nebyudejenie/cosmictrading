"""What the OpenAI-compatible adapter sends and records (S101). Groq's GPT-OSS models reason
before they answer, and the reasoning counts against the answer's length: they are asked for low
effort, without the reasoning in the response, so the JSON answer fits its limit. A refusal keeps
the provider's own short error code for the record, never its message."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from cosmic.ai.adapters.openai_compat import AIError, OpenAICompatAdapter

ANSWER = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 1}}
REFUSED = json.dumps(
    {
        "error": {
            "message": "Failed to generate JSON. Please adjust your prompt. See 'failed_generation'",
            "type": "invalid_request_error",
            "code": "json_validate_failed",
            "failed_generation": '{"direction": "UP", "why": "cut off',
        }
    }
)


async def _send(provider: str, model: str, status: int = 200, text: str = "") -> dict[str, Any]:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, text=text)
        return httpx.Response(200, json=ANSWER)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = OpenAICompatAdapter(
            provider_key=provider, base_url="https://ai.example/v1", api_key="k", client=client
        )
        await adapter.chat(model=model, messages=[{"role": "user", "content": "x"}], max_tokens=9)
    return sent


async def test_groqs_gpt_oss_models_reason_briefly_and_leave_the_reasoning_out() -> None:
    sent = await _send("groq", "openai/gpt-oss-120b")
    assert (sent["reasoning_effort"], sent["include_reasoning"]) == ("low", False)
    assert sent["response_format"] == {"type": "json_object"} and sent["max_tokens"] == 9
    for provider, model in (("groq", "llama-big"), ("openrouter", "openai/gpt-oss-120b:free")):
        other = await _send(provider, model)
        assert "reasoning_effort" not in other and "include_reasoning" not in other


async def test_a_refusal_records_the_providers_code_and_nothing_it_generated() -> None:
    with pytest.raises(AIError) as refused:
        await _send("groq", "openai/gpt-oss-120b", 400, REFUSED)
    assert str(refused.value) == "request refused HTTP 400 (json_validate_failed)"
    assert refused.value.http_status == 400
    assert "cut off" not in str(refused.value) and "adjust" not in str(refused.value)
    for text in ("not json", '{"error": "a sentence, not a code"}', '{"error": {"code": "A B"}}'):
        with pytest.raises(AIError, match=r"^request refused HTTP 404$"):
            await _send("openrouter", "vendor/gone:free", 404, text)
