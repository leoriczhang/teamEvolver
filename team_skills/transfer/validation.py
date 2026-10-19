"""Strict public transfer input; credentials are encrypted before acceptance."""
import re

from team_skills.library.editor import validate_skill_name

from .channels import http_url
from .git import GitAdapter
from .packages import MAX_ARCHIVE_BYTES, MAX_SKILLS, TransferError, safe_path

FIELDS = {
    "zip": {"zip_b64", "name", "single"},
    "git": {"url", "branch", "commit", "path", "name", "username", "token", "secret_ref", "mode",
            "new_branch", "provider", "api_url", "repo_name", "namespace", "private", "message"},
    "marketplace": {"provider", "registry_url", "slug", "version", "download_url", "upload_url",
                    "file_field", "display_name", "changelog", "name", "token", "secret_ref"},
}


def validate_request(operation, body):
    if not isinstance(body, dict) or set(body) - {"channel", "options", "names", "conflict"}:
        raise TransferError("导入导出参数不合法")
    channel = body.get("channel")
    if not isinstance(channel, str) or channel not in FIELDS:
        raise TransferError("请选择 ZIP、Marketplace 或 Git 渠道")
    options = body.get("options", {})
    if not isinstance(options, dict) or set(options) - FIELDS[channel]:
        raise TransferError("渠道包含不支持的参数")
    for key, value in options.items():
        if key in {"private", "single"}:
            if type(value) is not bool:
                raise TransferError("private/single 必须为布尔值")
            continue
        limit = (MAX_ARCHIVE_BYTES + 2) // 3 * 4 if key == "zip_b64" else 8192
        if not isinstance(value, str) or len(value) > limit or (key != "zip_b64" and any(ord(c) < 32 for c in value)):
            raise TransferError("渠道参数格式或长度不合法")
        if key.endswith("_url") and value:
            http_url(value)
    if options.get("token") and options.get("secret_ref"):
        raise TransferError("token 与 secret_ref 只能选择一个")
    if options.get("path") not in {None, "", "."}:
        safe_path(options["path"])
    if options.get("name"):
        validate_skill_name(options["name"])
    if channel == "git":
        if operation == "import" or options.get("mode", "new_branch") == "new_branch":
            GitAdapter()._url(options.get("url"))
        if options.get("commit") and not re.fullmatch(r"[0-9a-fA-F]{7,40}", options["commit"]):
            raise TransferError("commit 必须是 7–40 位十六进制提交 ID")
        if options.get("mode", "new_branch") not in {"new_branch", "new_repository"}:
            raise TransferError("Git mode 不合法")
    if operation == "export":
        names = body.get("names")
        if (not isinstance(names, list) or not 1 <= len(names) <= MAX_SKILLS
                or any(not isinstance(n, str) for n in names) or len(names) != len(set(names))):
            raise TransferError("请选择不重复的 Skill 名称")
        for name in names:
            validate_skill_name(name)
    elif not isinstance(body.get("conflict", "replace"), str) or body.get("conflict", "replace") not in {
        "replace", "skip", "error",
    }:
        raise TransferError("conflict 必须是 replace、skip 或 error")
    return {**body, "options": dict(options)}
