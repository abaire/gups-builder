"""Model access: request shape, structured calls with repair, fake, log, `models`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from gups_builder import cli
from gups_builder.config import Config, RoleConfig
from gups_builder.llm.client import HttpLlm, LlmError, Message
from gups_builder.llm.fake import FakeLlm
from gups_builder.llm.log import CallLog
from gups_builder.llm.structured import StructuredCallFailed, call_structured

SCHEMA_WORD = {
    "type": "object",
    "properties": {"word": {"type": "string"}},
    "required": ["word"],
}


class _Word(BaseModel):
    word: str


def _config(**role_kwargs: Any) -> Config:
    role_kwargs.setdefault("temperature", 0.5)
    rc = RoleConfig(**role_kwargs)
    return Config(logic=rc, prose=RoleConfig(temperature=0.5))


def _chat_reply(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": body["content"]}}]}
    )


def _mock(
    reply_fn: Callable[[httpx.Request, dict[str, Any]], httpx.Response],
) -> tuple[httpx.MockTransport, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append({"request": request, "body": json.loads(request.content)})
        return reply_fn(request, requests[-1]["body"])

    return httpx.MockTransport(handler), requests


def test_request_body_json_schema_mode() -> None:
    transport, requests = _mock(lambda r, b: _chat_reply({"content": "OK"}))
    llm = HttpLlm(_config(model="m1"), transport=transport)
    assert llm.complete("logic", [Message("user", "hi")], SCHEMA_WORD) == "OK"
    assert len(requests) == 1
    req = requests[0]["request"]
    assert req.method == "POST"
    assert str(req.url) == "http://127.0.0.1:11434/v1/chat/completions"
    assert "authorization" not in req.headers
    body = requests[0]["body"]
    assert body["model"] == "m1"
    assert body["temperature"] == 0.5
    assert body["max_tokens"] == 4096
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "reply", "schema": SCHEMA_WORD, "strict": True},
    }


def test_request_body_bearer_header_and_temperature() -> None:
    transport, requests = _mock(lambda r, b: _chat_reply({"content": "OK"}))
    llm = HttpLlm(_config(model="m1", api_key="k3y", temperature=0.9), transport=transport)
    llm.complete("logic", [Message("system", "s"), Message("user", "u")], None)
    req = requests[0]["request"]
    assert req.headers["authorization"] == "Bearer k3y"
    body = requests[0]["body"]
    assert body["temperature"] == 0.9
    assert "response_format" not in body
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


def test_request_body_json_object_mode_appends_schema() -> None:
    transport, requests = _mock(lambda r, b: _chat_reply({"content": "OK"}))
    llm = HttpLlm(_config(model="m1", structured="json_object"), transport=transport)
    llm.complete("logic", [Message("user", "plan")], SCHEMA_WORD)
    body = requests[0]["body"]
    assert body["response_format"] == {"type": "json_object"}
    tail = body["messages"][0]["content"]
    assert tail.startswith("plan\nReply with JSON matching this schema:")
    assert '"type": "object"' in tail
    assert '"word"' in tail


def test_request_body_none_mode_no_response_format_and_schema_appended() -> None:
    transport, requests = _mock(lambda r, b: _chat_reply({"content": "OK"}))
    llm = HttpLlm(_config(model="m1", structured="none"), transport=transport)
    llm.complete("logic", [Message("user", "plan")], SCHEMA_WORD)
    body = requests[0]["body"]
    assert "response_format" not in body
    assert "Reply with JSON matching this schema:" in body["messages"][0]["content"]


def test_empty_model_raises_llm_error_naming_role() -> None:
    llm = HttpLlm(Config(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(LlmError, match="the prose model is not configured"):
        llm.complete("prose", [Message("user", "hi")], None)


def test_http_500_raises_llm_error_with_role_url_status() -> None:
    transport, _ = _mock(lambda r, b: httpx.Response(500, text="boom"))
    llm = HttpLlm(_config(model="m1", api_key="k"), transport=transport)
    with pytest.raises(LlmError) as exc:
        llm.complete("logic", [Message("user", "hi")], None)
    message = str(exc.value)
    assert "logic" in message and "11434" in message and "500" in message


def test_timeout_raises_llm_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    llm = HttpLlm(_config(model="m1"), transport=httpx.MockTransport(handler))
    with pytest.raises(LlmError):
        llm.complete("logic", [Message("user", "hi")], None)


def test_400_on_json_schema_retried_once_as_json_object_and_logged(tmp_path: Path) -> None:
    log = CallLog(tmp_path / "log.jsonl")
    responses: list[int] = [400, 200]
    payloads = [
        {"content": '{"word": "ok"}'},
        {"content": '{"word": "ok"}'},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        status = responses.pop(0)
        if status == 400:
            return httpx.Response(400, text="response_format unsupported")
        return _chat_reply(payloads.pop(0))

    llm = HttpLlm(_config(model="m1"), log=log, transport=httpx.MockTransport(handler))
    assert (
        llm.complete("logic", [Message("user", "Reply with the word OK.")], SCHEMA_WORD)
        == '{"word": "ok"}'
    )
    lines = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["error"] == "json_schema rejected; used json_object"
    assert lines[0]["reply"] == '{"word": "ok"}'
    assert lines[0]["role"] == "logic"
    assert lines[0]["model"] == "m1"


def test_400_twice_raises_llm_error(tmp_path: Path) -> None:
    log = CallLog(tmp_path / "log.jsonl")
    llm = HttpLlm(
        _config(model="m1"),
        log=log,
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="no")),
    )
    with pytest.raises(LlmError):
        llm.complete("logic", [Message("user", "hi")], SCHEMA_WORD)
    assert (tmp_path / "log.jsonl").exists()


def test_call_structured_first_try_succeeds() -> None:
    llm = FakeLlm({"logic": ['{"word": "ok"}']})
    result = call_structured(llm, "logic", [Message("user", "reply")], _Word, max_repairs=3)
    assert result.model_dump() == {"word": "ok"}
    assert llm.calls == [("logic", [Message("user", "reply")], _Word.model_json_schema())]


def test_call_structured_repair_after_invalid_json() -> None:
    llm = FakeLlm({"logic": ["not json at all", '{"word": "ok"}']})
    result = call_structured(llm, "logic", [Message("user", "reply")], _Word, max_repairs=3)
    assert result.model_dump() == {"word": "ok"}
    assert len(llm.calls) == 2
    _, assistant, user = llm.calls[1][1]
    assert assistant.role == "assistant"
    assert assistant.content == "not json at all"
    assert user.content.startswith("That reply was not valid:\n- ")
    assert user.content.endswith("Reply again with the corrected JSON only.")
    assert llm.calls[0] == ("logic", [Message("user", "reply")], _Word.model_json_schema())


def test_call_structured_repair_after_extra_check() -> None:
    llm = FakeLlm({"logic": ['{"word": "no"}', '{"word": "ok"}']})

    def check(reply: _Word) -> list[str]:
        return [] if reply.word == "ok" else ["word must be ok"]

    result = call_structured(
        llm, "logic", [Message("user", "reply")], _Word, max_repairs=3, extra_check=check
    )
    assert result.model_dump() == {"word": "ok"}
    assert len(llm.calls) == 2
    assert "word must be ok" in llm.calls[1][1][-1].content


def test_call_structured_exhausts_repairs_and_raises() -> None:
    llm = FakeLlm({"logic": ["bad", "still bad"]})
    with pytest.raises(StructuredCallFailed) as exc:
        call_structured(llm, "logic", [Message("user", "reply")], _Word, max_repairs=1)
    assert exc.value.attempts == ["bad", "still bad"]
    assert len(exc.value.errors) == 2
    assert all(error.startswith("- ") for error in exc.value.errors)
    assert len(llm.calls) == 2


def test_call_structured_strips_code_fence() -> None:
    llm = FakeLlm({"logic": ['```json\n{"word": "ok"}\n```']})
    result = call_structured(llm, "logic", [Message("user", "reply")], _Word, max_repairs=3)
    assert result.model_dump() == {"word": "ok"}
    assert len(llm.calls) == 1


def test_fake_llm_records_calls_and_fails_when_out_of_replies() -> None:
    llm = FakeLlm({"logic": ["a"], "prose": ["b"]})
    assert llm.complete("logic", [Message("user", "1")], None) == "a"
    assert llm.complete("prose", [Message("user", "2")], SCHEMA_WORD) == "b"
    assert llm.calls == [
        ("logic", [Message("user", "1")], None),
        ("prose", [Message("user", "2")], SCHEMA_WORD),
    ]
    with pytest.raises(AssertionError, match="logic ran out of scripted replies"):
        llm.complete("logic", [Message("user", "3")], None)


def test_call_log_one_json_line_per_record(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = CallLog(path)
    log.record("logic", "m1", [Message("user", "hi")], "reply", 1.25, None)
    log.record(
        "prose", "m2", [Message("user", "bye")], "", 0.5, "json_schema rejected; used json_object"
    )
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["role"] == "logic"
    assert first["model"] == "m1"
    assert first["messages"] == [{"role": "user", "content": "hi"}]
    assert first["reply"] == "reply"
    assert first["seconds"] == 1.25
    assert first["error"] is None
    assert "ts" in first
    assert second["error"] == "json_schema rejected; used json_object"


def test_models_ok_for_both_roles(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "response_format" in body:
            return _chat_reply({"content": '{"word": "ok"}'})
        return _chat_reply({"content": "OK"})

    requests: list[dict[str, Any]] = []

    def counting_handler(request: httpx.Request) -> httpx.Response:
        requests.append({"request": request, "body": json.loads(request.content)})
        return handler(request)

    transport = httpx.MockTransport(counting_handler)
    monkeypatch.setattr(cli, "_make_client", lambda _config: HttpLlm(_config, transport=transport))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc: int = cli.main(
        [
            "--world",
            str(tmp_path / "world"),
            "--logic-url",
            "http://127.0.0.1:11434/v1",
            "--logic-model",
            "logic-model",
            "--prose-url",
            "http://127.0.0.1:11434/v1",
            "--prose-model",
            "prose-model",
            "models",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "logic: OK (" in out
    assert "prose: OK (" in out
    assert "logic: json_schema OK" in out
    assert "prose: json_schema OK" in out
    assert len(requests) == 4


def test_models_reports_refused_json_schema(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, text="response_format unsupported")
        return _chat_reply({"content": "OK"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_make_client", lambda _config: HttpLlm(_config, transport=transport))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc = cli.main(
        [
            "--world",
            str(tmp_path / "world"),
            "--logic-url",
            "http://127.0.0.1:11434/v1",
            "--logic-model",
            "logic-model",
            "--prose-url",
            "http://127.0.0.1:11434/v1",
            "--prose-model",
            "prose-model",
            "models",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "logic: json_schema not honoured (400 from" in out
    assert 'structured = "json_object"' in out
    assert "prose: json_schema not honoured (400 from" in out


def test_models_refused_reply_that_does_not_validate(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "response_format" in body:
            return _chat_reply({"content": "just text, no JSON"})
        return _chat_reply({"content": "OK"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_make_client", lambda _config: HttpLlm(_config, transport=transport))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc = cli.main(
        [
            "--world",
            str(tmp_path / "world"),
            "--logic-url",
            "http://127.0.0.1:11434/v1",
            "--logic-model",
            "logic-model",
            "--prose-url",
            "http://127.0.0.1:11434/v1",
            "--prose-model",
            "prose-model",
            "models",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "logic: json_schema not honoured (reply is not valid JSON)" in out


def test_models_plain_request_failure_exits_1(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport, _ = _mock(lambda r, b: httpx.Response(500, text="down"))
    monkeypatch.setattr(cli, "_make_client", lambda _config: HttpLlm(_config, transport=transport))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc = cli.main(
        [
            "--world",
            str(tmp_path / "world"),
            "--logic-url",
            "http://127.0.0.1:11434/v1",
            "--logic-model",
            "logic-model",
            "--prose-url",
            "http://127.0.0.1:11434/v1",
            "--prose-model",
            "prose-model",
            "models",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "logic: FAILED:" in out
    assert "prose: FAILED:" in out


def test_json_object_fallback_appends_the_schema() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, text="unsupported")
        return _chat_reply({"content": '{"word": "ok"}'})

    requests: list[dict[str, Any]] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return handler(request)

    llm = HttpLlm(_config(model="m1"), transport=httpx.MockTransport(recording))
    llm.complete("logic", [Message("user", "plan")], SCHEMA_WORD)
    first, second = requests
    assert first["messages"] == [{"role": "user", "content": "plan"}]
    assert second["response_format"] == {"type": "json_object"}
    assert second["messages"][-1]["content"].startswith(
        "plan\nReply with JSON matching this schema:"
    )
    assert llm.last_fallback is True


def test_schema_without_a_user_message_is_sent_as_one() -> None:
    transport, requests = _mock(lambda r, b: _chat_reply({"content": "OK"}))
    llm = HttpLlm(_config(model="m1", structured="none"), transport=transport)
    llm.complete("logic", [Message("system", "be terse")], SCHEMA_WORD)
    messages = requests[0]["body"]["messages"]
    assert messages[0] == {"role": "system", "content": "be terse"}
    assert messages[1]["role"] == "user"
    assert messages[1]["content"].startswith("Reply with JSON matching this schema:")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>proxy error</html>"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    ],
)
def test_unusable_200_body_raises_llm_error(response: httpx.Response) -> None:
    llm = HttpLlm(_config(model="m1"), transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(LlmError, match="logic"):
        llm.complete("logic", [Message("user", "hi")], None)


def test_models_reports_json_schema_rejected_even_when_fallback_works(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        kind = body.get("response_format", {}).get("type")
        if kind == "json_schema":
            return httpx.Response(400, text="json_schema unsupported")
        return _chat_reply({"content": '{"word": "OK"}' if kind else "OK"})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli, "_make_client", lambda _config: HttpLlm(_config, transport=transport))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc = cli.main(
        ["--world", str(tmp_path / "world"), "--logic-model", "a", "--prose-model", "b", "models"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "logic: json_schema OK" not in out
    assert "logic: json_schema not honoured (400 from http://127.0.0.1:11434/v1)" in out
    assert "prose: json_schema not honoured (400 from http://127.0.0.1:11434/v1)" in out
