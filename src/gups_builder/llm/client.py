"""A chat client for OpenAI-compatible servers, used through the LlmClient protocol."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx

if TYPE_CHECKING:
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


class LlmClient(Protocol):
    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str: ...


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

    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str:
        rc = self._config.logic if role == "logic" else self._config.prose
        self.last_fallback = False
        if not rc.model:
            msg = f"the {role} model is not configured"
            raise LlmError(msg)
        headers = _auth_headers(rc)
        if rc.structured != "json_schema" and json_schema is not None:
            messages = _append_schema(messages, json_schema)
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
        return content, None

    def list_models(self, role: Role) -> list[str]:
        """Query the OpenAI-compatible ``/models`` endpoint and return sorted model ids."""
        rc = self._config.logic if role == "logic" else self._config.prose
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


def _auth_headers(rc: RoleConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {rc.api_key}"} if rc.api_key else {}
