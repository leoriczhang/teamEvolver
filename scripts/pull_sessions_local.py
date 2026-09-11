"""Pull Langfuse sessions to local disk: raw backup first, then convert.

Usage:
    python scripts/pull_sessions_local.py --limit 700

Behavior:
  1. List session ids from Langfuse (newest first), skipping ids already
     present in the raw backup dir.
  2. For each new session: fetch (session, full traces) and save the RAW
     payload to the raw dir (backup) + append to manifest.json.
  3. Convert with the configured trace mapper (no truncation) and save the
     converted session dict to the converted dir.

Everything is written locally — nothing is pushed to OpenViking.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teamEvolver.integrations.langfuse_client import (  # noqa: E402
    LangfuseClient,
    LangfuseError,
)
from teamEvolver.integrations.langfuse_convert import convert_langfuse_session  # noqa: E402
from teamEvolver.integrations.langfuse_mapper import build_trace_mapper_from_config  # noqa: E402
from teamEvolver.integrations.langfuse_pull import (  # noqa: E402
    build_filters_from_config,
    sanitize_session_id,
)

CONFIG_PATH = Path.home() / ".teamEvolver" / "config.yaml"
RAW_DIR = Path("/Users/z/Documents/trae_projects/openclaw_sessions_raw")
CONVERTED_DIR = Path("/Users/z/Documents/trae_projects/openclaw_sessions_converted")
WORKERS = 4
RETRIES = 3


def load_config() -> SimpleNamespace:
    data = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    lf = data.get("langfuse") or {}
    attrs = {f"langfuse_{k}": v for k, v in lf.items()}
    return SimpleNamespace(**attrs)


def fetch_with_retry(client: LangfuseClient, session_id: str, trace_name: str):
    last_exc: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return client.fetch_session_with_traces(session_id, trace_name=trace_name)
        except LangfuseError as exc:
            last_exc = exc
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise last_exc  # type: ignore[misc]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=700, help="max NEW sessions to pull")
    parser.add_argument("--trace-name", default="openclaw-turn")
    parser.add_argument(
        "--reconvert",
        action="store_true",
        help="re-convert every raw file in the raw dir (skips listing/pulling)",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=0, help="override client timeout seconds")
    args = parser.parse_args()

    config = load_config()
    mapper = build_trace_mapper_from_config(config)
    if args.timeout > 0:
        client = LangfuseClient(
            host=str(config.langfuse_host),
            public_key=str(config.langfuse_public_key),
            secret_key=str(config.langfuse_secret_key),
            timeout=args.timeout,
            page_limit=int(getattr(config, "langfuse_page_limit", 50) or 50),
        )
    else:
        client = LangfuseClient.from_config(config)
    filters = build_filters_from_config(config)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW_DIR / "manifest.json"

    have: set[str] = set()
    manifest: dict = {"count": 0, "fetched_at": "", "sessions": []}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        have = {sanitize_session_id(s["session_id"]) for s in manifest.get("sessions", [])}
    # also count raw files without manifest entries (idempotent on re-runs)
    for f in RAW_DIR.glob("session_*.json"):
        have.add(f.stem.removeprefix("session_"))

    if args.reconvert:
        raw_files = sorted(RAW_DIR.glob("session_*.json"))
        ok = failed = 0
        for rf in raw_files:
            try:
                payload = json.loads(rf.read_text())
                conv = convert_session_with_registry(
                    payload["session"], payload["traces"], mapper
                )
                (CONVERTED_DIR / rf.name).write_text(
                    json.dumps(conv, ensure_ascii=False), encoding="utf-8", errors="replace"
                )
                ok += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {rf.name}: {exc!r:.120}", flush=True)
        print(f"RECONVERT done={ok} failed={failed} total={len(raw_files)}", flush=True)
        return 1 if failed else 0

    listed = client.list_session_ids(filters, max_sessions=5000)
    todo = [sid for sid in listed if sanitize_session_id(sid) not in have]
    todo = todo[: args.limit]
    print(f"listed={len(listed)} already_local={len(have)} to_pull={len(todo)}", flush=True)

    lock = threading.Lock()
    manifest_sessions = manifest.setdefault("sessions", [])
    done = converted = failed = 0

    def process(session_id: str) -> None:
        nonlocal done, converted, failed
        try:
            session, traces = fetch_with_retry(client, session_id, args.trace_name)
            if not traces:
                # session has no <trace_name> traces (openapi/other types) —
                # not an openclaw turn; skip entirely, save nothing.
                with lock:
                    done += 1
                return
            fname = f"session_{sanitize_session_id(session_id)}.json"
            (RAW_DIR / fname).write_text(
                json.dumps({"session": session, "traces": traces}, ensure_ascii=False),
                encoding="utf-8", errors="replace",
            )
            conv = convert_langfuse_session(session, traces, mapper=mapper)
            (CONVERTED_DIR / fname).write_text(
                json.dumps(conv, ensure_ascii=False), encoding="utf-8", errors="replace"
            )
            with lock:
                done += 1
                converted += 1
                manifest_sessions.append(
                    {
                        "session_id": session_id,
                        "file": fname,
                        "created_at": str(session.get("createdAt") or ""),
                        "trace_count": len(traces),
                        "user_from_id": str(conv.get("user_alias") or ""),
                    }
                )
                manifest["count"] = len(manifest_sessions)
                manifest["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
                if done % 25 == 0:
                    print(f"progress {done}/{len(todo)} (failed={failed})", flush=True)
        except Exception as exc:  # noqa: BLE001
            with lock:
                failed += 1
                print(f"FAIL {session_id}: {exc!r:.120}", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, sid) for sid in todo]
        for _ in as_completed(futures):
            pass

    print(f"DONE pulled={done} converted={converted} failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
