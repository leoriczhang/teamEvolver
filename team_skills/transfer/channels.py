"""ZIP and HTTP marketplace adapters. Credentials live only for a request."""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx

from .packages import (
    MAX_ARCHIVE_BYTES,
    ExportResult,
    SkillPackage,
    TransferError,
    discover,
    make_zip,
    read_zip,
)


def http_url(value: Any) -> str:
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username or parsed.password or parsed.fragment
        or any(ord(c) < 32 for c in value)
    ):
        raise TransferError("请提供不含账号密码的 HTTP(S) 地址，凭据请填写在独立字段")
    return value


def request_bytes(method: str, url: str, *, limit: int = MAX_ARCHIVE_BYTES, **kwargs: Any) -> bytes:
    """Bounded HTTP response, with sanitized errors (no remote body or credentials)."""
    try:
        with httpx.Client(timeout=httpx.Timeout(90, connect=15), follow_redirects=True) as client:
            with client.stream(method, http_url(url), **kwargs) as response:
                if not response.is_success:
                    code = response.status_code
                    hint = {401: "认证失败，请检查 Token", 403: "无权限或该 Skill 不允许下载",
                            404: "地址或 Skill 不存在", 409: "目标或版本已存在",
                            429: "请求频率过高，请稍后重试"}.get(code, "远端操作失败")
                    raise TransferError(f"{hint}（HTTP {code}）", 502)
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise TransferError("远端响应大小超出限制", 413)
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise TransferError("无法连接远端或请求超时，请检查地址及网络", 502) from exc


def request_json(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    data = request_bytes(method, url, limit=1024 * 1024, **kwargs)
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise TransferError("远端未返回有效 JSON", 502) from exc
    if not isinstance(result, dict) or result.get("ok") is False or result.get("success") is False:
        raise TransferError("远端返回失败结果", 502)
    return result


class ZipAdapter:
    def read(self, options: Mapping[str, Any]) -> list[SkillPackage]:
        value = options.get("zip_b64", "")
        if not isinstance(value, str) or len(value) > (MAX_ARCHIVE_BYTES + 2) // 3 * 4:
            raise TransferError("ZIP 大小不能超过 64 MiB", 413)
        try:
            data = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TransferError("zip_b64 必须是有效的 base64") from exc
        packages = discover(read_zip(data), name=str(options.get("name") or ""))
        if options.get("single") and len(packages) != 1:
            raise TransferError("请使用批量导入接口")
        return packages

    def write(self, packages: Sequence[SkillPackage], options: Mapping[str, Any]) -> ExportResult:
        filename = f"{packages[0].name}.zip" if len(packages) == 1 else "skills.zip"
        return ExportResult({"channel": "zip", "exported": [p.name for p in packages]}, make_zip(packages), filename)


class MarketplaceAdapter:
    """ClawHub v1 or a custom marketplace exchanging ZIP via GET/multipart POST."""
    def _settings(self, options: Mapping[str, Any]) -> tuple[str, str, dict[str, str]]:
        provider = str(options.get("provider") or "clawhub")
        if provider not in {"clawhub", "http"}:
            raise TransferError("市场 provider 必须是 clawhub 或 http")
        base = http_url(options.get("registry_url") or "https://clawhub.ai")
        token = str(options.get("token") or "")
        if "\n" in token or "\r" in token:
            raise TransferError("Token 格式不合法")
        return provider, base, {"Authorization": f"Bearer {token}"} if token else {}

    def read(self, options: Mapping[str, Any]) -> list[SkillPackage]:
        provider, base, headers = self._settings(options)
        slug = str(options.get("slug") or "").strip()
        version = str(options.get("version") or "").strip()
        if provider == "http":
            url = http_url(options.get("download_url"))
            data = request_bytes("GET", url, headers=headers)
        else:
            if not slug:
                raise TransferError("请填写市场 Skill 标识 slug")
            params = {"slug": slug}
            if version:
                params["version"] = version
            data = request_bytes("GET", f"{base}/api/v1/download", params=params, headers=headers)
        # Hosted ClawHub skills return ZIP. GitHub-backed entries may instead
        # return a public-github descriptor; don't interpret JSON as an archive.
        if data.lstrip().startswith(b"{"):
            raise TransferError("该市场条目返回 Git 来源，请使用 Git 渠道填写对应仓库地址")
        return discover(read_zip(data), name=str(options.get("name") or ""),
                        origin={"provider": provider, "slug": slug, "version": version})

    def write(self, packages: Sequence[SkillPackage], options: Mapping[str, Any]) -> ExportResult:
        provider, base, headers = self._settings(options)
        if provider == "http":
            url = http_url(options.get("upload_url"))
            field = str(options.get("file_field") or "file")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\[\]-]{0,63}", field):
                raise TransferError("市场上传文件字段名不合法")
            request_json("POST", url, headers=headers,
                         files={field: ("skills.zip", make_zip(packages), "application/zip")},
                         data={"names": json.dumps([p.name for p in packages])})
            return ExportResult({"channel": "marketplace", "exported": [p.name for p in packages]})
        if len(packages) != 1:
            raise TransferError("ClawHub 每次上传一个 Skill")
        if not headers:
            raise TransferError("上传市场需要 Token")
        version = str(options.get("version") or "").strip()
        semver = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
        if not re.fullmatch(semver, version):
            raise TransferError("请填写语义版本号，例如 1.0.0")
        item = packages[0]
        payload = {"slug": str(options.get("slug") or item.name),
                   "displayName": str(options.get("display_name") or item.name),
                   "version": version, "changelog": str(options.get("changelog") or "Export from teamEvolver"),
                   "tags": ["latest"]}
        request_json("POST", f"{base}/api/v1/skills", headers=headers,
                     data={"payload": json.dumps(payload)},
                     files=[("files[]", (path, data, "application/octet-stream"))
                            for path, data in sorted(item.files.items())])
        return ExportResult({"channel": "marketplace", "exported": [item.name],
                             "slug": payload["slug"], "version": version})
