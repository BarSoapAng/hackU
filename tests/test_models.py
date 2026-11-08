import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import ModelGateway, ProviderError, input_upper_bound, provider_error


def clients():
    return (SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))),
            SimpleNamespace(responses=SimpleNamespace(create=AsyncMock())))


def response(text="Open at nine. [hours.md > Opening]", status="completed", content=None):
    return {"status": status, "output": [{"type": "message", "status": "completed",
            "content": content if content is not None else [{"type": "output_text", "text": text}]}]}


def test_project_uses_openai_low_reasoning_and_isolated_context():
    nemotron, openai = clients()
    nemotron.chat.completions.create.side_effect = RuntimeError("builder outage")
    openai.responses.create.return_value = response()
    gateway = ModelGateway(nemotron, openai)
    chunks = [{"filename": "hours.md", "heading": "Opening", "text": "Open at nine."}]
    answer = asyncio.run(gateway.answer("Be friendly.", [{"role": "user", "content": "Hello"}], chunks, "When?"))
    assert answer.endswith("[hours.md > Opening]")
    nemotron.chat.completions.create.assert_not_called()
    request = openai.responses.create.call_args.kwargs
    assert request["model"] == "gpt-5.6-luna"
    assert request["reasoning"] == {"effort": "low"}
    assert request["store"] is False and request["max_output_tokens"] == 2048
    assert "tools" not in request
    assert "Historical answers are not current sources" in request["input"][0]["content"]
    assert "untrusted content" in request["input"][0]["content"]
    assert request["input"][-1]["content"] == "When?"
    assert input_upper_bound({"input": request["input"]}) <= 12000


@pytest.mark.parametrize(("result", "kind"), [
    (response(status="incomplete"), "incomplete"),
    (response("   "), "empty"),
    (response(content=[{"type": "refusal", "refusal": "No"}]), "refusal"),
])
def test_project_rejects_unfinished_empty_or_refused_outputs(result, kind):
    nemotron, openai = clients()
    openai.responses.create.return_value = result
    with pytest.raises(ProviderError) as failure:
        asyncio.run(ModelGateway(nemotron, openai).answer("", [], [{"filename": "a.md", "text": "a"}], "a"))
    assert failure.value.kind == kind
    assert openai.responses.create.await_count == 1


def test_empty_vault_does_not_call_a_provider():
    nemotron, openai = clients()
    assert "notes aren’t ready" in asyncio.run(ModelGateway(nemotron, openai).answer("", [], [], "Anything?"))
    openai.responses.create.assert_not_called()


@pytest.mark.parametrize('preference', ['Reply in one message.', 'Use up to three short texts when natural.'])
def test_project_message_style_reaches_model_with_source_citations(preference):
    nemotron, openai = clients()
    text = 'Open at nine. [hours.md > Opening]<next_message>See you there!'
    openai.responses.create.return_value = response(text)
    result = asyncio.run(ModelGateway(nemotron, openai).answer(preference, [],
        [{'filename': 'hours.md', 'heading': 'Opening', 'text': 'Open at nine.'}], 'When?'))
    request = openai.responses.create.call_args.kwargs
    assert request['input'][1]['content'] == 'Project preferences:\n' + preference
    assert 'honor a single-message preference' in request['input'][0]['content']
    assert result == text


def test_context_trimming_keeps_question_and_current_source():
    nemotron, openai = clients()
    openai.responses.create.return_value = response()
    gateway = ModelGateway(nemotron, openai)
    asyncio.run(gateway.answer("", [{"role": "assistant", "content": "old" * 10000}],
        [{"filename": "fresh.md", "text": "current " * 20000}], "latest question"))
    request = openai.responses.create.call_args.kwargs
    assert input_upper_bound({"input": request["input"]}) <= 12000
    assert request["input"][-1]["content"] == "latest question"
    assert "fresh.md" in request["input"][-2]["content"]
    assert not any("oldold" in row["content"] for row in request["input"])


def test_builder_tool_contract():
    nemotron, openai = clients()
    nemotron.chat.completions.create.return_value = {"choices": [{"finish_reason": "tool_calls", "message": {
        "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "create_project", "arguments": '{"name":"Bakery"}'}}]}}]}
    tools = [{"type": "function", "function": {"name": "create_project", "parameters": {"type": "object"}}}]
    result = asyncio.run(ModelGateway(nemotron, openai).builder([{"role": "user", "content": "Create Bakery"}], tools))
    assert result == {"text": "", "tool_calls": [{"id": "call-1", "name": "create_project", "arguments": {"name": "Bakery"}}]}
    request = nemotron.chat.completions.create.call_args.kwargs
    assert request["model"] == "nemotron-builder" and request["max_tokens"] == 2048
    assert request["parallel_tool_calls"] is False
    assert request["temperature"] == 0
    assert request["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    openai.responses.create.assert_not_called()


@pytest.mark.parametrize(("name", "arguments"), [("reset_history", "{}"), ("create_project", "[]"), ("create_project", "not-json")])
def test_builder_rejects_unknown_or_malformed_tools(name, arguments):
    nemotron, openai = clients()
    nemotron.chat.completions.create.return_value = {"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"id": "c", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}
    with pytest.raises(ProviderError, match="tool"):
        asyncio.run(ModelGateway(nemotron, openai).builder([{"role": "user", "content": "hi"}],
            [{"function": {"name": "create_project"}}]))


def test_builder_trims_whole_old_turn_without_orphaning_tool_results():
    nemotron, openai = clients()
    nemotron.chat.completions.create.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": "Ready"}}]}
    messages = [{"role": "system", "content": "Policy"}, {"role": "user", "content": "old" * 10000},
                {"role": "assistant", "tool_calls": [{"id": "a"}]}, {"role": "tool", "tool_call_id": "a", "content": "result"},
                {"role": "user", "content": "current"}]
    asyncio.run(ModelGateway(nemotron, openai).builder(messages, []))
    assert nemotron.chat.completions.create.call_args.kwargs["messages"] == [messages[0], messages[-1]]
    assert len(messages) == 5


def test_oversize_current_turn_is_rejected_before_provider_call():
    nemotron, openai = clients()
    with pytest.raises(ProviderError) as failure:
        asyncio.run(ModelGateway(nemotron, openai).builder([{"role": "user", "content": "x" * 12000}], []))
    assert failure.value.kind == "input_limit"
    nemotron.chat.completions.create.assert_not_called()


def test_unavailable_model_fails_without_fallback_or_automatic_retry():
    class Unavailable(Exception):
        status_code = 404
    nemotron, openai = clients()
    openai.responses.create.side_effect = Unavailable("sensitive provider body")
    with pytest.raises(ProviderError) as failure:
        asyncio.run(ModelGateway(nemotron, openai).answer("", [], [{"filename": "a.md", "text": "a"}], "a"))
    assert failure.value.kind == "unavailable_model" and not failure.value.retryable
    assert "sensitive" not in str(failure.value)
    assert openai.responses.create.await_count == 1
    nemotron.chat.completions.create.assert_not_called()


def test_rate_limit_and_timeout_are_retryable_but_adapter_never_retries():
    class RateLimit(Exception):
        status_code = 429
    assert provider_error(RateLimit()).retryable
    assert provider_error(TimeoutError()).retryable


def test_clients_use_separate_credentials_and_disable_sdk_retries(monkeypatch):
    import openai
    recorded = []
    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kwargs: recorded.append(kwargs) or object())
    ModelGateway.from_credentials(nemotron_base_url="http://127.0.0.1:8000/v1", nemotron_api_key="builder-secret", openai_api_key="project-secret")
    assert recorded[0]["api_key"] == "builder-secret" and recorded[1]["api_key"] == "project-secret"
    assert recorded[1]["base_url"] == "https://api.openai.com/v1"
    assert all(options["max_retries"] == 0 for options in recorded)


def test_real_sdk_transport_failure_makes_one_request_and_no_fallback():
    import httpx
    from openai import AsyncOpenAI

    async def exercise():
        requests = []
        def transport(request):
            requests.append(request)
            return httpx.Response(429, json={"error": {"message": "private input echoed", "type": "rate_limit", "code": "rate_limit"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
            client = AsyncOpenAI(api_key="openai-test", http_client=http, max_retries=0)
            nemotron, _ = clients()
            gateway = ModelGateway(nemotron, client)
            with pytest.raises(ProviderError) as failure:
                await gateway.answer("", [], [{"filename": "a.md", "text": "current"}], "question")
            assert failure.value.retryable
            assert "private" not in str(failure.value)
            assert len(requests) == 1
            assert requests[0].url.host == "api.openai.com"
            assert requests[0].url.path == "/v1/responses"
            nemotron.chat.completions.create.assert_not_called()
    asyncio.run(exercise())
