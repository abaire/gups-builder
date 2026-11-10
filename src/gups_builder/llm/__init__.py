"""Model access: the LlmClient protocol, the HTTP client, the fake, structured calls and the log."""

from .client import HttpLlm, LlmClient, LlmError, Message, Role
from .fake import FakeLlm
from .log import CallLog
from .structured import StructuredCallFailed, call_structured

__all__ = [
    "CallLog",
    "FakeLlm",
    "HttpLlm",
    "LlmClient",
    "LlmError",
    "Message",
    "Role",
    "StructuredCallFailed",
    "call_structured",
]
