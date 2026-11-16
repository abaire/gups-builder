"""Model access: the LlmClient protocol, the HTTP client, the fake, structured calls and the log."""

from gups_builder.llm.client import HttpLlm, LlmClient, LlmError, Message, Role
from gups_builder.llm.fake import FakeLlm
from gups_builder.llm.log import CallLog
from gups_builder.llm.structured import StructuredCallError, call_structured

__all__ = [
    "CallLog",
    "FakeLlm",
    "HttpLlm",
    "LlmClient",
    "LlmError",
    "Message",
    "Role",
    "StructuredCallError",
    "call_structured",
]
