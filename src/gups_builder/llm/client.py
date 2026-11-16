"""A chat client for OpenAI-compatible servers, used through the LlmClient protocol."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

    from gups_builder.config import Config, RoleConfig
    from gups_builder.llm.log import CallLog

Role = Literal["logic", "prose"]

_SCHEMA_REFUSED = "json_schema rejected; used json_object"


class LlmError(Exception):
    """A model request failed to make or was refused."""


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ContextUsage:
    """How much of a role's context window one call fills.

    ``tokens`` is the prompt, plus the reply once it is in; it is an estimate
    until the server reports its own count.
    """

    role: Role
    tokens: int
    window: int
    estimated: bool

    def text(self) -> str:
        percent = round(100 * self.tokens / self.window) if self.window > 0 else 0
        approx = "~" if self.estimated else ""
        return f"{self.role} ctx {approx}{_k(self.tokens)}/{_k(self.window)} ({percent}%)"


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def estimate_message_tokens(messages: list[Message], json_schema: dict[str, Any] | None) -> int:
    """A cheap estimate of a request's prompt: about four characters a token."""
    chars = sum(len(m.content) for m in messages)
    if json_schema is not None:
        chars += len(json.dumps(json_schema))
    return chars // 4 + 1


class LlmClient(Protocol):
    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str: ...

    def set_usage_listener(self, listener: Callable[[ContextUsage], None] | None) -> None:
        """Hear each call's context usage: an estimate as it is sent, the count once back."""
        ...

    def model_name(self, role: Role) -> str:
        """The model serving ``role``, for progress messages; empty when it has no name."""
        ...


def _append_schema(messages: list[Message], schema: dict[str, Any]) -> list[Message]:
    """The schema rides inside the last user message when the server has no response_format.

    With no user message to carry it, it is sent as one of its own.
    """
    text = "Reply with JSON matching this schema:\n" + json.dumps(schema, indent=2)
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].role == "user":
            out[i] = replace(out[i], content=out[i].content + "\n" + text)
            return out
    return [*out, Message("user", text)]


class HttpLlm:
    """One client serving both roles from a Config; replies are logged when a CallLog is given."""

    def __init__(
        self,
        config: Config,
        log: CallLog | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._config = config
        self._log = log
        client_kwargs: dict[str, Any] = {}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._http = httpx.Client(**client_kwargs)
        # Whether the last call fell back from json_schema to json_object, for the
        # `models` check: the fallback is invisible in the reply itself.
        self.last_fallback = False
        self._usage_listener: Callable[[ContextUsage], None] | None = None
        # The server's token count for the last reply, when it sent one.
        self._reported_tokens: int | None = None

    def set_usage_listener(self, listener: Callable[[ContextUsage], None] | None) -> None:
        self._usage_listener = listener

    def _report_usage(self, role: Role, rc: RoleConfig, tokens: int, *, estimated: bool) -> None:
        if self._usage_listener is not None:
            self._usage_listener(ContextUsage(role, tokens, rc.context_tokens, estimated))

    def _report_sent(
        self,
        role: Role,
        rc: RoleConfig,
        messages: list[Message],
        json_schema: dict[str, Any] | None,
    ) -> int:
        """Report the estimate for a call about to be sent, and return it."""
        estimate = estimate_message_tokens(messages, json_schema)
        self._reported_tokens = None
        self._report_usage(role, rc, estimate, estimated=True)
        return estimate

    def _report_replied(self, role: Role, rc: RoleConfig, estimate: int, reply: str) -> None:
        """Report the server's count for the call just back, or the estimate plus the reply."""
        if self._reported_tokens is not None:
            self._report_usage(role, rc, self._reported_tokens, estimated=False)
        else:
            self._report_usage(role, rc, estimate + len(reply) // 4, estimated=True)

    def model_name(self, role: Role) -> str:
        return self._role_config(role).model

    def _role_config(self, role: Role) -> RoleConfig:
        return self._config.logic if role == "logic" else self._config.prose

    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str:
        rc = self._role_config(role)
        self.last_fallback = False
        if not rc.model:
            msg = f"the {role} model is not configured"
            raise LlmError(msg)
        headers = _auth_headers(rc)
        if rc.structured != "json_schema" and json_schema is not None:
            messages = _append_schema(messages, json_schema)
        estimate = self._report_sent(role, rc, messages, json_schema)
        started = time.monotonic()
        try:
            reply, error = self._send(role, rc, messages, json_schema, headers)
            if error is not None and json_schema is not None:
                # json_object carries no schema of its own, so it rides in the messages.
                messages = _append_schema(messages, json_schema)
                reply, error = self._send(role, rc, messages, None, headers, force_json_object=True)
                if error is None:
                    error = _SCHEMA_REFUSED
                    self.last_fallback = True
        except LlmError as failure:
            if self._log is not None:
                self._log.record(
                    role, rc.model, messages, "", time.monotonic() - started, str(failure)
                )
            raise
        if self._log is not None:
            self._log.record(role, rc.model, messages, reply, time.monotonic() - started, error)
        self._report_replied(role, rc, estimate, reply)
        return reply

    def _send(
        self,
        role: Role,
        rc: RoleConfig,
        messages: list[Message],
        json_schema: dict[str, Any] | None,
        headers: dict[str, str],
        *,
        force_json_object: bool = False,
    ) -> tuple[str, str | None]:
        payload: dict[str, Any] = {
            "model": rc.model,
            "messages": [asdict(m) for m in messages],
            "temperature": rc.temperature,
            "max_tokens": rc.max_tokens,
        }
        if force_json_object:
            payload["response_format"] = {"type": "json_object"}
        elif json_schema is not None and rc.structured == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": json_schema, "strict": True},
            }
        elif json_schema is not None and rc.structured == "json_object":
            payload["response_format"] = {"type": "json_object"}
        try:
            response = self._http.post(
                f"{rc.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=rc.timeout_s,
            )
        except httpx.HTTPError as error:
            msg = f"{role} request to {rc.base_url} failed: {error}"
            raise LlmError(msg) from error
        if (
            response.status_code == HTTPStatus.BAD_REQUEST
            and not force_json_object
            and payload.get("response_format", {}).get("type") == "json_schema"
        ):
            return "", _SCHEMA_REFUSED
        if response.status_code != HTTPStatus.OK:
            msg = (
                f"{role} request to {rc.base_url} got HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
            raise LlmError(msg)
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as error:
            msg = f"{role} request to {rc.base_url} got an unusable reply: {response.text[:200]}"
            raise LlmError(msg) from error
        if not isinstance(content, str):
            msg = f"{role} request to {rc.base_url} got a reply with no text content"
            raise LlmError(msg)
        self._reported_tokens = _total_tokens(response)
        return content, None

    def list_models(self, role: Role) -> list[str]:
        """Query the OpenAI-compatible ``/models`` endpoint and return sorted model ids."""
        rc = self._role_config(role)
        url = f"{rc.base_url.rstrip('/')}/models"
        try:
            response = self._http.get(
                url,
                headers=_auth_headers(rc),
                timeout=min(rc.timeout_s, 15.0),
            )
        except httpx.HTTPError as error:
            msg = f"{role} request to {url} failed: {error}"
            raise LlmError(msg) from error
        if response.status_code != HTTPStatus.OK:
            msg = f"{role} request to {url} got HTTP {response.status_code}: {response.text[:200]}"
            raise LlmError(msg)
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError) as error:
            msg = f"{role} request to {url} got an unusable reply: {response.text[:200]}"
            raise LlmError(msg) from error
        if not isinstance(data, list):
            msg = f"{role} request to {url} got an unusable reply: data is not a list"
            raise LlmError(msg)
        ids = [
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
        return sorted(ids)


def _total_tokens(response: httpx.Response) -> int | None:
    """The server's prompt plus reply token count, when its reply carries ``usage``."""
    usage = response.json().get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    parts = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    ints = [n for n in parts if isinstance(n, int)]
    return sum(ints) if ints else None


def _auth_headers(rc: RoleConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {rc.api_key}"} if rc.api_key else {}
