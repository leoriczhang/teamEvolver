"""Stable upstream field aliases for metadata extraction."""
import re

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_CONTEXT_TAG_RE = re.compile(r"<context_[^>]+>.*?</context_[^>]+>", re.DOTALL | re.IGNORECASE)

_REQUEST_CONTEXT_RE = re.compile(r"<request_context>.*?</request_context>", re.DOTALL | re.IGNORECASE)

_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+")

_TRUNCATED_USER_RE = re.compile(
    r'"(?:role|type)"\s*:\s*"(?:user|human)"\s*,\s*"content"\s*:\s*"((?:\\.|[^"\\])*)"',
    re.IGNORECASE,
)

_USER_KEYS = (
    "userids",
    "user_ids",
    "usercode",
    "user_code",
    "senderuserid",
    "sender_user_id",
    "senderid",
    "sender_id",
    "userid",
    "user_id",
    "sender",
    "user",
)

_TRACE_ID_KEYS = (
    "agenttraceid",
    "customtraceid",
    "biztraceid",
    "businesstraceid",
    "externaltraceid",
    "traceid",
    "trace_id",
    "requestid",
    "request_id",
    "reqid",
    "req_id",
)

_PROMPT_KEYS = (
    "sys.query",
    "user_query",
    "userquery",
    "query",
    "question",
    "prompt",
    "message",
    "keyword",
)

_TITLE_KEYS = (
    "title",
    "subject",
    "biztitle",
    "biz_title",
)

_USER_ROLES = {"user", "human"}

_INTERNAL_PROMPT_MARKERS = (
    "read heartbeat.md",
    "if nothing needs attention, reply heartbeat_ok",
)

_GENERIC_TITLE_NAMES = {
    "",
    "agent-call",
    "openclaw-turn",
    "trace",
    "turn",
    "session",
    "message",
    "langgraph",
    "a2a.receive",
    "spring_ai chat_client",
    "oc_agent_loop",
}
