"""Bounded capture of legacy child output. Arbitrary child payloads are suppressed."""

import logging
import re
import threading

from .logging_runtime import event, redact, register_secrets

logger = logging.getLogger("team_miner.subprocess")


def capture_subprocess(proc, env):
    register_secrets(env)

    def read():
        stream = proc.stdout
        if stream is None:
            return
        try:
            while True:
                chunk = stream.readline(4096)
                if not chunk:
                    break
                # Drain oversized lines without retaining unbounded payloads or logging continuations.
                size = len(chunk)
                truncated = not chunk.endswith(b"\n")
                while chunk and not chunk.endswith(b"\n"):
                    chunk = stream.readline(4096)
                    size += len(chunk)
                text = redact(chunk.decode("utf-8", errors="replace")) if not truncated else ""
                # Child may print prompts / model results. Emit only recognised runtime metadata.
                level = logging.ERROR if re.search(r"ERROR|Traceback|Exception", text) else logging.DEBUG
                kind = "error" if level == logging.ERROR else "output"
                if "Application startup complete" in text:
                    kind, level = "ready", logging.INFO
                elif "Application shutdown complete" in text:
                    kind, level = "stopped", logging.INFO
                event(
                    logger,
                    "miner.child_output",
                    level,
                    child_pid=proc.pid,
                    kind=kind,
                    truncated=truncated,
                    content="omitted",
                    bytes_read=size,
                )
        except Exception:
            logger.exception("miner.capture_failed")
        finally:
            stream.close()

    thread = threading.Thread(target=read, name="te-miner-log-reader", daemon=True)
    thread.start()
    return thread
