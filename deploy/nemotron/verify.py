"""Run live inference checks against the prepared Nemotron service."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    args = parser.parse_args()
    key = next(
        line.partition("=")[2].strip()
        for line in args.env_file.read_text().splitlines()
        if line.startswith("NEMOTRON_API_KEY=")
    )
    if not key:
        raise RuntimeError("Missing inference API key")

    def request(path, body=None, token=key):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(args.base_url.rstrip("/") + path, data, headers)
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)

    for token in (None, "invalid-verification-key"):
        try:
            request("/v1/models", token=token)
        except urllib.error.HTTPError as error:
            if error.code != 401:
                raise
        else:
            raise AssertionError("Unauthenticated model access was accepted")
    print("PASS: missing and invalid credentials rejected", flush=True)

    models = request("/v1/models")["data"]
    assert any(model["id"] == "nemotron-builder" for model in models)
    print("PASS: authenticated model discovery", flush=True)

    def chat(messages, **options):
        started = time.monotonic()
        result = request("/v1/chat/completions", {
            "model": "nemotron-builder",
            "messages": messages,
            "temperature": 0,
            "max_tokens": 512,
            "chat_template_kwargs": {"enable_thinking": False},
            **options,
        })
        choice = result["choices"][0]
        assert choice["finish_reason"] in ("stop", "tool_calls"), choice["finish_reason"]
        return choice["message"], round(time.monotonic() - started, 2), result.get("usage")

    answer, elapsed, usage = chat([{"role": "user", "content": "What is 2 + 2? Reply with only the digit."}])
    assert answer.get("content", "").strip() == "4", answer
    print(json.dumps({"check": "text_response", "seconds": elapsed, "usage": usage}), flush=True)

    tools = [{"type": "function", "function": {
        "name": "lookup_project",
        "description": "Look up a project title by its public code.",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    }}]
    messages = [{"role": "user", "content": "Use lookup_project to find the title of project DEMO42. Report its title after the tool returns."}]
    answer, elapsed, _ = chat(messages, tools=tools, tool_choice="auto")
    calls = answer.get("tool_calls", [])
    assert len(calls) == 1, answer
    call = calls[0]
    assert call["function"]["name"] == "lookup_project", call
    assert json.loads(call["function"]["arguments"]) == {"code": "DEMO42"}, call
    print(json.dumps({"check": "automatic_tool_call", "seconds": elapsed}), flush=True)

    messages.extend([
        {"role": "assistant", "content": answer.get("content"), "tool_calls": calls},
        {"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"code": "DEMO42", "title": "Brev verification Sprout"})},
    ])
    answer, elapsed, _ = chat(messages, tools=tools, tool_choice="none")
    assert "Brev verification Sprout" in (answer.get("content") or ""), answer
    print(json.dumps({"check": "tool_result_followup", "seconds": elapsed}), flush=True)

    def parallel_check(digit):
        messages = [{"role": "user", "content": (
            "Ignore this padding text:\n" + "blue sky. " * 3800
            + f"\nReply with only this digit: {digit}"
        )}]
        answer, elapsed, usage = chat(messages)
        assert (answer.get("content") or "").strip() == str(digit), answer
        assert 10000 <= usage["prompt_tokens"] <= 12000, usage
        return {"seconds": elapsed, "usage": usage}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(parallel_check, (7, 9)))
    print(json.dumps({"check": "two_concurrent_long_context_requests", "results": results}), flush=True)
    print("PASS: all live inference checks", flush=True)


if __name__ == "__main__":
    main()
