"""Separate channel transport headers from requests and sender identity."""
from __future__ import annotations

import copy
import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping

from teamEvolver.security.agent_principal import normalize_user_id

SUBMISSION_REVISION = "channel-submission-v2"
_HEADER = re.compile(r"\A\s*(<ChannelContext\b[^>]*?/>)", re.DOTALL)
_POLICY = re.compile(r"\A\s*<ContextReadingPolicy>.*?</ContextReadingPolicy>", re.DOTALL)
_BOT_MENTION = re.compile(
    r"\A@[A-Za-z0-9_.-]+(?:[ \t]+[A-Za-z0-9_.-]+){0,5}[ \t]+Bot\b[ \t]*"
    r"|\A@[A-Za-z0-9_.-]*Bot\b[ \t]*", re.IGNORECASE,
)


def channel_message(value):
    """Only the leading transport block has authority; body text stays data."""
    text = str(value or "")
    match = _HEADER.match(text)
    if not match or len(match.group(1)) > 16_384:
        return {}, text
    try:
        header = dict(ET.fromstring(match.group(1)).attrib)
    except ET.ParseError:
        return {}, text
    body = _POLICY.sub("", text[match.end():], count=1).strip()
    return header, _BOT_MENTION.sub("", body, count=1).strip()


def channel_identity(header):
    provider = str(header.get("provider") or "").strip()
    app_id = str(header.get("app_id") or "").strip()
    sender_id = str(header.get("sender_user_id") or header.get("sender_open_id") or "").strip()
    if not provider or not app_id or not sender_id:
        return None
    # Validate components independently so dotted namespaces remain unambiguous.
    for part in (provider, app_id, sender_id):
        if "." in part:
            raise ValueError("Invalid channel identity scope")
        normalize_user_id(part)
    return {
        "provider": provider, "app_id": app_id, "sender_id": sender_id,
        "id_type": "user_id" if header.get("sender_user_id") else "open_id",
        "tenant_key": str(header.get("tenant_key") or ""),
        "display_name": " ".join(str(header.get("sender_name") or "").split())[:120],
        "display_name_source": "channel_context" if header.get("sender_name") else "unavailable",
    }


def scoped_user_id(identity):
    return normalize_user_id(".".join(identity[key] for key in ("provider", "app_id", "sender_id")))


def submission_metadata(session, *, tagged_user_id="", default_user_id=""):
    """First main user turn owns the Session; later senders remain participants."""
    turns = [t for t in session.get("turns") or [] if isinstance(t, Mapping)]
    first_text = str(turns[0].get("prompt_text") or "") if turns else ""
    first = channel_message(first_text)[0]
    identity = channel_identity(first)
    if tagged_user_id:
        user_id, attribution = normalize_user_id(tagged_user_id), "tag"
    elif identity:
        user_id, attribution = scoped_user_id(identity), "channel_context"
    elif first or first_text.lstrip().startswith("<ChannelContext"):
        # A channel with missing identity must not become the connection operator.
        key = str(session.get("session_id") or "")
        user_id = "unknown." + hashlib.sha256(key.encode()).hexdigest()[:32]
        attribution = "channel_identity_unavailable"
    elif not default_user_id or default_user_id.startswith("unknown."):
        key = str((session.get("runtime_context") or {}).get("source_session_id") or session.get("session_id") or "")
        user_id = default_user_id or "unknown." + hashlib.sha256(key.encode()).hexdigest()[:32]
        attribution = "identity_unavailable"
    else:
        user_id, attribution = normalize_user_id(default_user_id), "connection_default"
    if identity:
        # A later header can supply a display name for the same initiating sender.
        for turn in turns[1:]:
            other = channel_identity(channel_message(turn.get("prompt_text"))[0])
            if (other and scoped_user_id(other) == scoped_user_id(identity)
                    and other["display_name"]):
                identity = other
                break
    meta = {
        "user_id": user_id, "user_attribution": attribution,
        "submission_revision": SUBMISSION_REVISION,
        "user_identity": identity or {},
        "user_display_name": (
            "未知提交人" if attribution in {"identity_unavailable", "channel_identity_unavailable"}
            else identity["display_name"] if identity and not tagged_user_id else ""
        ),
        "title": request_title(session.get("title")) or request_title(first_text),
    }
    return meta


def request_title(value):
    header, body = channel_message(value)
    # A truncated protocol header is never a useful display title.
    if body.lstrip().startswith(("<ChannelContext", "<ContextReadingPolicy")):
        return ""
    if header and body.lstrip().startswith("<Context"):
        return ""
    return " ".join(body.split())[:120]


def apply_submission(session, metadata):
    result = copy.deepcopy(session)
    result.setdefault("meta", {}).update(metadata)
    result.setdefault("runtime_context", {}).update(metadata)
    result["user_id"] = metadata["user_id"]
    result["user_alias"] = metadata.get("user_display_name") or metadata["user_id"]
    title = request_title(metadata.get("title") or result.get("title"))
    if not title:
        title = next((request_title(t.get("prompt_text")) for t in result.get("turns") or []
                      if request_title(t.get("prompt_text"))), "")
    result["title"] = title
    result["meta"]["title"] = title
    for turn in result.get("turns") or []:
        identity = channel_identity(channel_message(turn.get("prompt_text"))[0])
        if identity:
            turn["user_identity"] = identity
    return result
