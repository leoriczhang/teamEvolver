"""Process-owned, bounded logging. No business payloads belong in diagnostic events."""

from __future__ import annotations

import atexit
import contextvars
import json
import logging
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DEFAULTS = dict(
    level="INFO",
    console_enabled=True,
    file_enabled=True,
    directory="~/.teamEvolver",
    max_file_mb=100,
    retention_days=14,
    max_total_mb=2048,
)
ENV = {k: "TEAMEVOLVER_LOG_" + ("DIR" if k == "directory" else k.upper()) for k in DEFAULTS}
_context = contextvars.ContextVar("te_log_context", default={})
_runtime = None
_secrets: set[str] = set()
_secret_lock = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SENSITIVE = re.compile(r"(?i)(api[_-]?key|password|passwd|secret|authorization|cookie|access[_-]?token|root[_-]?key)")


def register_secrets(value):
    """Register configured secrets so even unlabelled exception text is protected."""
    if hasattr(value, "__dataclass_fields__"):
        value = vars(value)
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(str(key)) and isinstance(item, str) and len(item) >= 4:
                with _secret_lock:
                    _secrets.add(item)
            elif isinstance(item, dict):
                register_secrets(item)


def redact(value):
    text = ANSI.sub("", str(value))
    with _secret_lock:
        secrets = tuple(_secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[\w.+/=-]+", r"\1 [REDACTED]", text)
    text = re.sub(r"([a-zA-Z][\w+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1?[REDACTED]", text)
    # Values can be quoted, nested, or multiline; discard the rest of that field/line.
    text = re.sub(
        r"""(?ims)(["']?(?:[\w-]*(?:password|passwd|api[_-]?key|secret|token)|authorization|cookie|dsn|prompt|messages|source_text|response_body|request_body|content)["']?\s*[:=]\s*).*""",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", text)
    return text


def safe_code(value, default="UNCLASSIFIED"):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,100}", value) else default


@contextmanager
def log_context(**fields):
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def request_id():
    return _context.get().get("request_id", "")


def event(logger, name, level=logging.INFO, **fields):
    logger.log(level, name, extra={"event": name, "fields": fields, "context": dict(_context.get())})


class SafeFormatter(logging.Formatter):
    def format(self, record):
        stamp = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")
        message = redact(record.getMessage()).replace("\r", "\\r")
        if "Traceback (most recent call last):" in message:
            prefix, trace = message.split("Traceback (most recent call last):", 1)
            frames = []
            for line in trace.splitlines():
                if re.match(r'\s*File "[^"\n]+", line \d+, in ', line):
                    frames.append(line)
                elif re.match(r"^[\w.]+(?:Error|Exception)(?::|$)", line):
                    frames.append(line.split(":", 1)[0] + ": [details omitted]")
            message = prefix + "Traceback (most recent call last):\n" + "\n".join(frames)
        fields = {**getattr(record, "context", {}), **getattr(record, "fields", {})}
        safe_fields = {}
        for key, value in fields.items():
            if SENSITIVE.search(key) or key in {"prompt", "body", "content", "messages"}:
                value = "[REDACTED]"
            elif not isinstance(value, (bool, int, float, type(None))):
                value = redact(value)
            safe_fields[key] = value
        details = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in safe_fields.items())
        result = (
            f"{stamp} | {record.levelname:8} | {record.name} | pid={record.process} "
            f"event={getattr(record, 'event', 'log')} {message} {details}"
        ).rstrip()
        if record.exc_info:
            # Exception strings (e.g. Pydantic/jsonschema/HTTP errors) may contain full business payloads.
            # Keep frame locations and exception classes, omit source-code lines and arbitrary exception values.
            exc = record.exc_info[1]
            seen = set()
            while exc and id(exc) not in seen:
                seen.add(id(exc))
                frames = traceback.extract_tb(exc.__traceback__)
                result += "\nTraceback (most recent call last):\n" + "\n".join(
                    f'  File "{redact(f.filename)}", line {f.lineno}, in {f.name}' for f in frames
                )
                result += f"\n{type(exc).__name__}: {safe_code(getattr(exc, 'detail', None), '[details omitted]')}"
                exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
        result = redact(result)
        return result if len(result) <= 16384 else result[:16384] + " [log truncated]"


def resolve_settings(raw=None, *, log_file=None, environ=None, sources=None, process_id=None):
    env = os.environ if environ is None else environ
    raw = raw or {}
    cfg, origins = dict(DEFAULTS), {}
    for key, default in DEFAULTS.items():
        val = raw.get(key, default)
        origins[key] = "yaml" if key in raw else "default"
        if ENV[key] in env:
            val = env[ENV[key]]
            origins[key] = (sources or {}).get(ENV[key], "process_environment")
        try:
            if isinstance(default, bool):
                if str(val).lower() not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
                    raise ValueError(key)
                val = str(val).lower() in {"1", "true", "yes", "on"}
            elif isinstance(default, int):
                val = int(val)
                if val <= 0:
                    raise ValueError(key)
            else:
                val = str(val)
            cfg[key] = val
        except (ValueError, TypeError):
            raise ValueError(f"Invalid logging option: {key}") from None
    cfg["level"] = cfg["level"].upper()
    if cfg["level"] not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid logging option: level")
    path = Path(log_file).expanduser() if log_file else Path(cfg["directory"]).expanduser() / "teamEvolver.log"
    if log_file:
        cfg["file_enabled"] = True
        origins["directory"] = origins["file_enabled"] = "cli"
    if env.get("TEAMEVOLVER_MULTI_REPLICA", "").lower() in {"1", "true", "yes", "on"}:
        instance = re.sub(r"[^A-Za-z0-9_.-]", "_", env.get("TEAMEVOLVER_INSTANCE_ID") or socket.gethostname())[:80]
        # PID also prevents two workers on the same host from sharing the file.
        if not env.get("TEAMEVOLVER_INSTANCE_ID"):
            instance = f"{instance}-{process_id or os.getpid()}"
        path = path.parent / instance / path.name
    cfg["path"] = str(path.absolute())
    cfg["sources"] = origins
    return cfg


class RollingFile:
    """Only the writer thread touches this object or its files."""

    def __init__(self, cfg, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.path = Path(cfg["path"])
        self.stream = None
        self.lease = None
        self.day = ""
        self.size = 0
        self.pattern = re.compile(re.escape(self.path.name) + r"\.(\d{4}-\d{2}-\d{2})\.(\d{6,})$")

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.lease is None:
            lease = self.path.with_name(self.path.name + ".lock").open("a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    lease.seek(0)
                    if not lease.read(1):
                        lease.write(b"0")
                        lease.flush()
                    lease.seek(0)
                    msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.lease = lease
            except Exception:
                lease.close()
                raise
        existing = self.path.stat() if self.path.exists() else None
        self.stream = self.path.open("a", encoding="utf-8")
        self.day = datetime.fromtimestamp(existing.st_mtime if existing else self.clock()).strftime("%Y-%m-%d")
        self.size = existing.st_size if existing else 0
        self.cleanup()

    def archives(self):
        found = []
        for path in self.path.parent.iterdir():
            match = self.pattern.fullmatch(path.name)
            if match and path.is_file() and not path.is_symlink():
                found.append((match.group(1), int(match.group(2)), path))
        return sorted(found)

    def cleanup(self):
        archives = self.archives()
        total = self.path.stat().st_size + sum(p.stat().st_size for _, _, p in archives)
        cutoff = self.clock() - self.cfg["retention_days"] * 86400
        for day, _, path in archives:
            expired = datetime.strptime(day, "%Y-%m-%d").timestamp() < cutoff
            if expired or total > self.cfg["max_total_mb"] * 1024 * 1024:
                total -= path.stat().st_size
                path.unlink()

    def write(self, line):
        today = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        data = line + "\n"
        size = len(data.encode("utf-8"))
        if self.size and (today != self.day or self.size + size > self.cfg["max_file_mb"] * 1024 * 1024):
            self.close(release=False)
            seq = max((n for d, n, _ in self.archives() if d == self.day), default=0) + 1
            # Reserve destination exclusively, including after restart.
            while True:
                archive = self.path.with_name(f"{self.path.name}.{self.day}.{seq:06d}")
                try:
                    archive.touch(exist_ok=False)
                    break
                except FileExistsError:
                    seq += 1
            self.path.replace(archive)
            self.open()
            self.cleanup()
        self.stream.write(data)
        self.stream.flush()
        self.size += size
        self.day = today

    def close(self, *, release=True):
        try:
            if self.stream:
                self.stream.close()
        finally:
            self.stream = None
            if release and self.lease:
                self.lease.close()
                self.lease = None


class QueueHandler(logging.Handler):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime

    def emit(self, record):
        record.context = dict(_context.get())
        try:
            # Queue only sanitised text; do not retain traceback frames or request objects.
            line = self.runtime.formatter.format(record)
            self.runtime.queue.put_nowait(line)
        except queue.Full:
            self.runtime.dropped += 1
            if record.levelno >= logging.ERROR:
                self.runtime.emergency(line)
        except Exception:
            self.runtime.dropped += 1
            self.runtime.emergency("logging.format_failed")


class LogRuntime:
    def __init__(self, cfg, *, capacity=8192, clock=time.time):
        self.cfg, self.clock = cfg, clock
        self.queue = queue.Queue(maxsize=capacity)
        self.formatter = SafeFormatter()
        self.file = RollingFile(cfg, clock)
        self.dropped = self.file_missed = 0
        self.last_error = None
        self.file_state = "starting" if cfg["file_enabled"] else "disabled"
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.run, name="te-log-writer", daemon=True)
        self.handler = QueueHandler(self)
        self.ready = threading.Event()
        self.shutdown_deadline = float("inf")

    def emergency(self, text):
        try:
            if not re.match(r"^\d{4}-\d{2}-\d{2}T", text):
                stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                text = f"{stamp} | WARNING  | teamEvolver.logging | pid={os.getpid()} event={text}"
            sys.stderr.write(redact(text) + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def failed(self, exc):
        reason = f"{type(exc).__name__}: {redact(exc)}"
        changed = self.file_state != "degraded" or self.last_error != reason
        self.file_state, self.last_error = "degraded", reason
        try:
            self.file.close()
        except Exception:
            pass
        if changed:
            self.emergency(
                f"logging.file_degraded path={self.cfg['path']} reason={reason} file_missed={self.file_missed}"
            )

    def maintenance(self):
        if not self.cfg["file_enabled"]:
            return
        try:
            if not self.file.stream:
                recovering = self.file_state == "degraded"
                self.file.open()
                self.file_state = "active"
                if recovering:
                    self.emergency(f"logging.file_recovered path={self.cfg['path']} file_missed={self.file_missed}")
            else:
                self.file.cleanup()
        except Exception as exc:
            self.failed(exc)

    def run(self):
        self.maintenance()
        self.ready.set()
        last_check, reported = self.clock(), 0
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                if time.monotonic() >= self.shutdown_deadline:
                    self.dropped += self.queue.qsize()
                    self.emergency(f"logging.shutdown_discarded count={self.queue.qsize()}")
                    break
                if self.clock() - last_check >= 60:
                    self.maintenance()
                    if self.dropped != reported or self.file_state == "degraded":
                        self.emergency(
                            f"logging.health state={self.file_state} dropped={self.dropped} "
                            f"file_missed={self.file_missed}"
                        )
                        reported = self.dropped
                    last_check = self.clock()
                try:
                    line = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if self.cfg["console_enabled"]:
                        self.emergency(line)
                    if self.cfg["file_enabled"]:
                        if self.file.stream:
                            try:
                                self.file.write(line)
                            except Exception as exc:
                                self.file_missed += 1
                                self.failed(exc)
                                if not self.cfg["console_enabled"]:
                                    self.emergency(line)
                        else:
                            self.file_missed += 1
                            if not self.cfg["console_enabled"]:
                                self.emergency(line)
                finally:
                    self.queue.task_done()
        finally:
            try:
                self.file.close()
            except Exception as exc:
                self.failed(exc)

    def start(self):
        self.thread.start()
        self.ready.wait(timeout=2)

    def close(self, timeout=3):
        self.shutdown_deadline = time.monotonic() + timeout
        self.stopping.set()
        self.thread.join(timeout=timeout + 0.3)

    def status(self):
        return {
            **self.cfg,
            "file_state": self.file_state,
            "last_write_error": self.last_error,
            "dropped_count": self.dropped,
            "file_missed_count": self.file_missed,
            "queue_capacity": self.queue.maxsize,
            "queue_depth": self.queue.qsize(),
            "timezone": datetime.now().astimezone().tzname(),
        }


def configure(raw=None, *, log_file=None, config_file=None, env_file=None, sources=None, environ=None):
    global _runtime
    cfg = resolve_settings(raw, log_file=log_file, sources=sources, environ=environ)
    cfg.update(config_file=str(config_file or ""), env_file=str(env_file or ""))
    shutdown()
    register_secrets(dict(os.environ))
    runtime = LogRuntime(cfg)
    root = logging.getLogger()
    # Own the service handlers, including Uvicorn's default non-propagating handlers.
    root.handlers.clear()
    root.setLevel(cfg["level"])
    root.addHandler(runtime.handler)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    # HTTP transport DEBUG traces can contain raw header bytes and cookies.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    os.environ["TEAMEVOLVER_RUNTIME_LOG_PATH"] = cfg["path"]
    _runtime = runtime
    runtime.start()
    event(
        logging.getLogger(__name__),
        "logging.configured",
        path=cfg["path"],
        timezone=runtime.status()["timezone"],
        config_file=cfg["config_file"],
        env_file=cfg["env_file"],
        sources=cfg["sources"],
        file_state=runtime.file_state,
        effective_level=cfg["level"],
        file_enabled=cfg["file_enabled"],
        console_enabled=cfg["console_enabled"],
        max_file_mb=cfg["max_file_mb"],
        retention_days=cfg["retention_days"],
        max_total_mb=cfg["max_total_mb"],
        level=logging.INFO,
    )
    return runtime


def shutdown():
    global _runtime
    if _runtime:
        logging.getLogger().removeHandler(_runtime.handler)
        _runtime.close()
        _runtime = None


def logging_status():
    return _runtime.status() if _runtime else {"file_state": "not_configured", "path": None, "dropped_count": 0}


atexit.register(shutdown)
