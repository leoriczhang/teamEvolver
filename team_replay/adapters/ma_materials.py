"""Deliver pinned inputs inside a sandbox when native resource copies exceed quota."""

import copy
import json

from ..hooks import ReplayUnsupported

SANDBOX_MATERIAL_ROOT = "/tmp/te-replay-inputs"

_BOOTSTRAP = '''
import hashlib
import json
import os
from pathlib import Path
import tempfile
import urllib.request
from urllib.parse import urlsplit

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def main():
    opener = urllib.request.build_opener(NoRedirect())
    root = Path(MOUNT_ROOT).resolve()
    root.mkdir(parents=True, exist_ok=True)
    for item in MATERIALS:
        request = urllib.request.Request(
            BASE_URL + "/files/" + item["file_id"],
            headers={"Authorization": "Bearer " + os.environ["ARK_API_KEY"]})
        with opener.open(request, timeout=30) as response:
            metadata = response.read(1024 * 1024 + 1)
        if len(metadata) > 1024 * 1024:
            raise ValueError("Material metadata exceeds limit")
        native = json.loads(metadata)
        if native.get("id") != item["file_id"] or native.get("status") != "active":
            raise ValueError("Original material unavailable")
        url = native["download_url"]
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid material download URL")
        # A fresh request carries no MA credential to the signed download host.
        with opener.open(urllib.request.Request(url), timeout=60) as response:
            raw = response.read(item["size"] + 1)
        if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Frozen material content mismatch")
        target = (root / item["mount_path"]).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("Material path escapes uploads")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
            temporary = output.name
            output.write(raw)
        try:
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    print("TE Replay material hashes verified:", len(MATERIALS))

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Exceptions can include signed URLs. Never print their messages.
        raise SystemExit("TE Replay material bootstrap failed: " + type(exc).__name__) from None
'''


def bootstrap_source(base_url, materials):
    manifest = [{key: m[key] for key in ("file_id", "mount_path", "sha256", "size")} for m in materials]
    return (f"BASE_URL = {base_url!r}\nMATERIALS = {manifest!r}\n"
            f"MOUNT_ROOT = {SANDBOX_MATERIAL_ROOT!r}\n" + _BOOTSTRAP)


def bootstrap_environment(config, base_url, materials):
    result = copy.deepcopy(config)
    script = bootstrap_source(base_url, materials)
    result["setup_script"] = (
        "python3 - <<'TE_REPLAY_MATERIALS' || exit 1\n" + script + "\nTE_REPLAY_MATERIALS\n"
        + (config.get("setup_script") or "")
    )
    if len(json.dumps(result, ensure_ascii=False).encode()) > 65535:
        raise ReplayUnsupported("Replay material bootstrap exceeds MA Environment config limit")
    return result
