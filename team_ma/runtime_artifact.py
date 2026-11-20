"""Standalone, stdlib-only installer embedded in immutable MA Environments."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024


def relative_path(value):
    if (not isinstance(value, str) or not value or value.startswith("/") or "\\" in value
            or any(p in {"", ".", ".."} for p in value.split("/"))
            or any(ord(c) < 32 for c in value)):
        raise ValueError("Unsafe runtime artifact path")
    return value


def sha256(value):
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("Invalid runtime artifact SHA-256")
    return value


def validate_artifact(artifact):
    if not isinstance(artifact, dict):
        raise ValueError("OpenViking runtime artifact is required")
    url = urllib.parse.urlsplit(artifact.get("base_url", ""))
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.query or url.fragment or "\\" in url.netloc
            or any(ord(c) < 33 for c in artifact.get("base_url", ""))):
        raise ValueError("Runtime OpenViking base_url must be HTTPS without credentials")
    relative_path(url.path.strip("/")) if url.path.strip("/") else None
    uri = artifact.get("uri", "")
    if not isinstance(uri, str) or not uri.startswith("viking://user/"):
        raise ValueError("Runtime OpenViking URI must be a user resource")
    relative_path(uri[len("viking://"):])
    if "/resources/" not in uri or "?" in uri or "#" in uri:
        raise ValueError("Invalid runtime OpenViking resource URI")
    sha256(artifact.get("sha256"))
    if type(artifact.get("bytes")) is not int or not 1 <= artifact["bytes"] <= MAX_BYTES:
        raise ValueError("Invalid runtime archive byte limit")
    rows = artifact.get("files")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 500:
        raise ValueError("Runtime artifact requires 1–500 manifest files")
    seen, total = set(), 0
    for row in rows:
        path = relative_path(row["path"])
        if path in seen:
            raise ValueError("Duplicate runtime manifest path")
        seen.add(path)
        sha256(row.get("sha256"))
        if type(row.get("bytes")) is not int or not 0 <= row["bytes"] <= MAX_BYTES:
            raise ValueError("Invalid runtime file byte limit")
        total += row["bytes"]
    if total > MAX_BYTES:
        raise ValueError("Runtime unpacked byte limit exceeded")
    return rows


def mapped_files(artifact, mappings):
    rows = validate_artifact(artifact)
    selected = {}
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("runtime_binding.paths is required")
    for mapping in mappings:
        source = relative_path(mapping["bundle_prefix"].rstrip("/")) + "/"
        target = relative_path(mapping["workspace_prefix"].rstrip("/")) + "/"
        for row in rows:
            if row["path"].startswith(source):
                name = target + row["path"][len(source):]
                if name in selected:
                    raise ValueError("Runtime paths overlap")
                selected[name] = row
    if not selected:
        raise ValueError("Runtime artifact contains no files for configured paths")
    for name in selected:
        if any(parent.as_posix() in selected for parent in Path(name).parents):
            raise ValueError("Runtime file conflicts with parent path")
    return selected


def artifact_tree(selected):
    rows = [(path, row["sha256"]) for path, row in sorted(selected.items())]
    return hashlib.sha256(json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_archive(artifact, target):
    key = os.environ.get("OPENVIKING_API_KEY")
    if not key:
        raise ValueError("Runtime download requires OPENVIKING_API_KEY")
    url = artifact["base_url"].rstrip("/") + "/api/v1/content/download?" + urllib.parse.urlencode({
        "uri": artifact["uri"],
    })
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    start = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            if response.status != 200:
                raise ValueError("Runtime archive download failed")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != artifact["bytes"]:
                raise ValueError("Runtime download byte count mismatch")
            total = 0
            with target.open("wb") as output:
                while True:
                    chunk = response.read(min(65536, artifact["bytes"] + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > artifact["bytes"] or time.monotonic() - start > 180:
                        raise ValueError("Runtime download exceeded byte/time limit")
                    output.write(chunk)
            if total != artifact["bytes"]:
                raise ValueError("Runtime download byte count mismatch")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError("OpenViking runtime download failed; no redirects are allowed") from None


def valid_archive(path, artifact):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != artifact["bytes"]:
        return False
    return hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def unpack_verified(path, artifact, stage):
    rows = {row["path"]: row for row in artifact["files"]}
    seen = set()
    with tarfile.open(path, mode="r|xz") as archive:
        for item in archive:
            if not item.isfile() or item.name not in rows or item.name in seen:
                raise ValueError("Runtime archive contains unexpected or non-regular entry")
            row = rows[item.name]
            if item.size != row["bytes"]:
                raise ValueError("Runtime file byte count mismatch: " + item.name)
            data = archive.extractfile(item).read(item.size + 1)
            if len(data) != item.size or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Runtime file hash mismatch: " + item.name)
            target = stage / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            seen.add(item.name)
    if seen != set(rows):
        raise ValueError("Runtime archive is missing manifest files")


def install_artifact(artifact, mappings, working):
    selected = mapped_files(artifact, mappings)
    root = Path(working).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cache = root / ".team-evolver-runtime-cache"
    if cache.is_symlink():
        raise ValueError("Runtime cache must not be a symlink")
    cache.mkdir(exist_ok=True)
    lock = cache / "download.lock"
    if lock.is_symlink():
        raise ValueError("Runtime cache lock must not be a symlink")
    with lock.open("a") as handle, tempfile.TemporaryDirectory(dir=cache) as temporary:
        fcntl.flock(handle, fcntl.LOCK_EX)
        cached = cache / (artifact["sha256"] + ".tar.xz")
        hit = valid_archive(cached, artifact)
        archive = cached if hit else Path(temporary) / "archive.tar.xz"
        if not hit:
            download_archive(artifact, archive)
        if not valid_archive(archive, artifact):
            raise ValueError("Runtime archive SHA-256 mismatch")
        stage = Path(temporary) / "files"
        unpack_verified(archive, artifact, stage)
        targets = []
        for name, row in selected.items():
            target = root / name
            if not target.resolve().is_relative_to(root) or target.is_symlink():
                raise ValueError("Runtime target escapes workspace")
            targets.append((stage / row["path"], target))
        if not hit:
            os.replace(archive, cached)
        for source, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Mappings may reuse the same source in more than one destination.
            shutil.copyfile(source, target)
        proof = {"source": "openviking", "uri": artifact["uri"], "archive_sha256": artifact["sha256"],
                 "runtime_tree_sha256": artifact_tree(selected), "cache_hit": hit, "files": len(selected)}
        (root / ".team-evolver-runtime-artifact.json").write_text(json.dumps(proof))
        return proof
