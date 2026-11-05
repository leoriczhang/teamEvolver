"""Deterministic runtime overlays constructed from the same published Skill bytes."""

import base64
import copy
import gzip
import hashlib
import io
import lzma
import shlex
import tarfile
from pathlib import PurePosixPath

from .schemas import digest


def safe_relative(path):
    if (not path or path.startswith("/") or "\\" in path
            or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise ValueError("Unsafe runtime Bundle path")
    return path


def mounted_runtime_script(binding, chosen, origins):
    """Copy only verified files from the Session's immutable Skill mount."""
    name = binding.get("skill_name", "")
    if not isinstance(name, str) or not name or "/" in name:
        raise ValueError("skill_mount requires a single Skill name")
    safe_relative(name)
    manifest = [(origins[p], p, hashlib.sha256(data).hexdigest()) for p, data in sorted(chosen.items())]
    return (
        "python3 - <<'TE_RUNTIME_MOUNT'\n"
        "import hashlib\nfrom pathlib import Path\n"
        f"source = Path({'/mnt/skills/' + name!r}).resolve()\n"
        f"manifest = {manifest!r}\n"
        "verified = []\n"
        "for relative, target, expected in manifest:\n"
        "    path = (source / relative).resolve()\n"
        "    if not path.is_relative_to(source):\n"
        "        raise SystemExit('Mounted runtime path escapes Skill')\n"
        "    data = path.read_bytes()\n"
        "    if hashlib.sha256(data).hexdigest() != expected:\n"
        "        raise SystemExit('Mounted runtime hash mismatch: ' + relative)\n"
        "    verified.append((target, data))\n"
        "for relative, data in verified:\n"
        "    target = Path(relative)\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    target.write_bytes(data)\n"
        "TE_RUNTIME_MOUNT\n"
    )


def bind_runtime(environment_config, binding, files):
    """Return a complete Environment config and hash without executing candidate code."""
    config = copy.deepcopy(environment_config)
    if not binding:
        return config, ""
    if config.get("type") != "cloud":
        raise ValueError("runtime_binding requires a cloud Environment")
    mappings = binding.get("paths")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("runtime_binding.paths is required")
    working = binding.get("working_directory", "")
    if (not working.startswith("/workspace/") or ".." in PurePosixPath(working).parts
            or any(ord(c) < 32 for c in working)):
        raise ValueError("Runtime working_directory must be within /workspace")
    source_type = binding.get("source", "embedded")
    if source_type not in ("embedded", "skill_mount"):
        raise ValueError("Runtime source must be embedded or skill_mount")
    chosen, origins = {}, {}
    for mapping in mappings:
        source = safe_relative(mapping["bundle_prefix"].rstrip("/")) + "/"
        target = safe_relative(mapping["workspace_prefix"].rstrip("/")) + "/"
        for path, data in files.items():
            safe_relative(path)
            if path.startswith(source):
                output = target + path[len(source):]
                if output in chosen:
                    raise ValueError("Runtime paths overlap")
                chosen[output] = data
                origins[output] = path
    if not chosen:
        raise ValueError("Runtime Bundle contains no files for configured paths")
    compression = binding.get("archive_compression", "gzip")
    if compression not in ("gzip", "xz"):
        raise ValueError("Runtime archive_compression must be gzip or xz")
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w") as archive:
        for path, data in sorted(chosen.items()):
            item = tarfile.TarInfo(path)
            item.size, item.mode, item.mtime = len(data), 0o644, 0
            archive.addfile(item, io.BytesIO(data))
    if compression == "xz":
        compressed = lzma.compress(content.getvalue(), preset=9 | lzma.PRESET_EXTREME)
        extract = (
            'python3 -c "import lzma,sys; '
            'sys.stdout.buffer.write(lzma.decompress(sys.stdin.buffer.read()))" | tar -x'
        )
    else:
        compressed = gzip.compress(content.getvalue(), mtime=0)
        extract = "tar -xz"
    encoded = base64.b64encode(compressed).decode()
    tree = digest([(p, hashlib.sha256(b).hexdigest()) for p, b in sorted(chosen.items())])
    extraction = (mounted_runtime_script(binding, chosen, origins) if source_type == "skill_mount"
                  else f"printf '%s' '{encoded}' | base64 -d | {extract}\n")
    # Prerequisites precede the source setup too: it may call its bootstrap.
    pre_setup = binding.get("pre_setup") or ""
    if not isinstance(pre_setup, str) or len(pre_setup.encode()) > 32768:
        raise ValueError("Runtime pre_setup must be at most 32 KiB")
    replace_setup = binding.get("replace_setup_script", False)
    if type(replace_setup) is not bool:
        raise ValueError("Runtime replace_setup_script must be boolean")
    script = pre_setup + "\n" + ("" if replace_setup else config.get("setup_script") or "")
    script += (
        "\nset -euo pipefail\nmkdir -p " + shlex.quote(working) + "\ncd " + shlex.quote(working) + "\n"
        + extraction
        + str(binding.get("bootstrap") or "") + "\n"
        + "printf '%s' " + shlex.quote(tree) + " > .team-evolver-runtime-sha256\n"
    )
    ready = binding.get("ready_file")
    if ready:
        safe_relative(ready)
        script += (
            "python3 - <<'TE_RUNTIME_READY'\nimport json\nfrom pathlib import Path\n"
            + "root = Path(" + repr(working) + ")\n"
            + "state = json.loads((root / " + repr(ready) + ").read_text())\n"
            + "assert state.get('ready') is True\n"
            + "proof = {'ready': True, 'runtime_tree_sha256': " + repr(tree) + ", 'state': state}\n"
            + "output = Path('/mnt/session/outputs/team-evolver-runtime-ready.json')\n"
            + "output.parent.mkdir(parents=True, exist_ok=True)\n"
            + "output.write_text(json.dumps(proof))\nTE_RUNTIME_READY\n"
        )
    if len(script.encode()) > 262144:
        raise ValueError("Runtime setup_script exceeds MA 256 KiB; use mounted Files")
    config["setup_script"] = script
    return config, tree
