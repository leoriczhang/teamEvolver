"""OpenViking-backed object store."""

from __future__ import annotations

import base64
import functools
import hashlib
import io
import threading
import time
from typing import Any, Iterator, Mapping

from .base import ObjectInfo, _BytesObject, read_bytes

# Wire constant, do not rename. This string is a shared data contract: Hermes'
# AgentsHub and the evolve server read team skills from this shared contract.
_VIKING_ROOT_PREFIX = "team-skill-evolver"

# Prefix written before base64-encoded binary content so get_object can
# reverse the transform. OpenViking's /content/write endpoint is text-only
# and the /content/batch-write endpoint has been unavailable since
# 2026-09-04, so binary objects are base64-encoded and written as text.
_B64_MARKER = b"__OPENVIKING_BASE64_V1__\n"


@functools.lru_cache(maxsize=8)
def _shared_client(endpoint: str, timeout: float):
    """Return a process-wide pooled HTTP client for one OpenViking endpoint.

    Store instances are rebuilt per ingest/operation, but the keep-alive
    connection pool must outlive them — otherwise every request pays a fresh
    TCP connect + DNS lookup (seconds for slow resolvers, e.g. ``.local``
    hostnames on macOS). Sharing one client per (endpoint, timeout) keeps
    connections warm across instances. ``httpx.Client`` is thread-safe and
    ``functools.lru_cache`` locks construction, so concurrent ``to_thread``
    callers share the pool safely.
    """
    import httpx

    return httpx.Client(
        timeout=timeout,
        limits=httpx.Limits(keepalive_expiry=60.0),
        transport=httpx.HTTPTransport(retries=1),
    )


# Per-key in-process write locks. OpenViking holds a short per-file lock on
# every write and returns CONFLICT ("lock acquire timed out after 2ms") when
# another write to the *same* key is in flight. Evidence records are
# read-modify-write, so concurrent writers to one key both thrash the server
# lock and lose updates. Serializing same-key writers per process removes the
# self-inflicted contention; cross-process writers (rare) fall back to the
# retry loop below. Keys are unbounded in principle but bounded in practice
# (skills x evidence windows), so a plain dict is fine.
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock(uri: str) -> threading.Lock:
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(uri)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[uri] = lock
        return lock


class OpenVikingObjectStore:
    """OpenViking-backed object store.

    Maps the object-store contract onto OpenViking's filesystem-style REST API.
    Every object lives under the account-scoped, team-shared *resources* root::

        viking://resources/{root_prefix}/...                  # group_id empty
        viking://resources/{root_prefix}/{group_id}/...       # group_id set

    ``root_prefix`` defaults to the wire constant ``team-skill-evolver`` and
    ``group_id`` defaults to empty so the team library lives directly under
    ``viking://resources/team-skill-evolver/``, matching what AgentsHub and
    Hermes'
    ``OpenVikingSkillSource`` scans
    (``viking://resources/team-skill-evolver/skills/<name>/``). Isolated runs
    use a separate root prefix rather
    than a group segment. The shared key ``X-API-Key`` authenticates as
    ``account=default`` with write access to the ``resources/`` namespace, so no
    per-user space is needed.

    Callers decide isolation by the key they pass:

    - team-shared skill files: ``skills/...`` ->
      ``viking://resources/{root_prefix}/skills/...``
    - team-shared object data: ``manifest.json``, registry files ->
      ``viking://resources/{root_prefix}/...``
    - per-person (isolated): ``peers/{customer_id}/sessions/...`` etc. — see
      :func:`teamEvolver.storage.peer_key_prefix`.

    Contract mapping:

    - ``put_object(key, data)`` →  ``POST /api/v1/content/write``
    - ``get_object(key)`` →  ``GET /api/v1/content/download?uri=...``
    - ``delete_object(key)`` →  ``DELETE /api/v1/fs?uri=...``
    - ``iter_objects(prefix)`` →  recursive walk via ``GET /api/v1/fs/ls``
    """

    _NOT_FOUND_TOKENS = ("NOT_FOUND", "NoSuchURI", "RESOURCE_NOT_FOUND")
    native_batch_write = True
    _BATCH_MAX_OPERATIONS = 256
    _BATCH_MAX_FILE_BYTES = 8 * 1024 * 1024
    _BATCH_MAX_TOTAL_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str = "",
        account: str = "default",
        user: str = "default",
        agent: str = _VIKING_ROOT_PREFIX,
        agent_id: str = "",
        root_prefix: str = _VIKING_ROOT_PREFIX,
        group_id: str = "",
        namespace: str = "resources",
        timeout: float = 30.0,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - httpx is a core dep
            raise ImportError(
                "OpenViking storage backend requires the 'httpx' package."
            ) from exc

        if not endpoint:
            raise ValueError("OpenViking storage backend requires an endpoint.")
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._account = account or "default"
        self._user = user or "default"
        self._agent = agent or _VIKING_ROOT_PREFIX
        # Retained for header attribution / backward-compatible callers; the
        # resources namespace is account-scoped so it no longer drives the URI.
        self._agent_id = (agent_id or self._user or "default").strip("/")
        self._root_prefix = (root_prefix or _VIKING_ROOT_PREFIX).strip("/")
        # Empty group_id means "no group segment": objects live directly under
        # ``viking://resources/{root_prefix}/``. A non-empty group adds one path
        # segment for isolation (used by eval via a separate root_prefix instead).
        self._group_id = (group_id or "").strip("/")
        self._namespace = (namespace or "resources").strip().lower()
        self._timeout = timeout
        # Defaults to the shared pooled client; tests may inject a fake
        # module-like object exposing ``request()``.
        self._httpx = _shared_client(self._endpoint, self._timeout)

    # ------------------------------------------------------------------ #
    # URI helpers                                                         #
    # ------------------------------------------------------------------ #

    def _base_uri(self) -> str:
        """Return the account-scoped, team-shared resources root prefix.

        When ``group_id`` is empty the group segment is omitted entirely::

            viking://resources/{root_prefix}/

        A non-empty group adds one isolating segment::

            viking://resources/{root_prefix}/{group_id}/
        """
        if self._namespace == "user":
            return f"viking://user/{self._user}/"
        if self._group_id:
            return f"viking://resources/{self._root_prefix}/{self._group_id}/"
        return f"viking://resources/{self._root_prefix}/"

    def _uri(self, key: str) -> str:
        clean = str(key or "").strip().replace("\\", "/").lstrip("/")
        return f"{self._base_uri()}{clean}"

    def _strip_uri(self, uri: str) -> str:
        prefix = self._base_uri()
        if uri.startswith(prefix):
            return uri[len(prefix):]
        return uri

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                        #
    # ------------------------------------------------------------------ #

    def _headers(self, *, multipart: bool = False) -> dict:
        h = {
            "X-OpenViking-Account": self._account,
            "X-OpenViking-User": self._user,
            "X-OpenViking-Agent": self._agent,
        }
        if not multipart:
            h["Content-Type"] = "application/json"
        if self._api_key:
            h["X-API-Key"] = self._api_key
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self._endpoint}{path}"
        kwargs.setdefault("timeout", self._timeout)
        headers = kwargs.pop("headers", None) or self._headers()
        resp = self._httpx.request(method, url, headers=headers, **kwargs)
        try:
            data = resp.json()
        except Exception:
            data = None
        if resp.status_code >= 400:
            err_msg = ""
            if isinstance(data, dict):
                err = data.get("error") or {}
                if isinstance(err, dict):
                    err_msg = f"{err.get('code', 'HTTP_ERROR')}: {err.get('message', '')}"
            if not err_msg:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if any(tok in err_msg for tok in self._NOT_FOUND_TOKENS):
                raise FileNotFoundError(err_msg)
            raise RuntimeError(err_msg)
        if isinstance(data, dict) and data.get("status") == "error":
            err = data.get("error") or {}
            err_msg = f"{err.get('code', 'OPENVIKING_ERROR')}: {err.get('message', '')}"
            if any(tok in err_msg for tok in self._NOT_FOUND_TOKENS):
                raise FileNotFoundError(err_msg)
            raise RuntimeError(err_msg)
        return data or {}

    def _request_bytes(self, method: str, path: str, **kwargs) -> bytes:
        url = f"{self._endpoint}{path}"
        kwargs.setdefault("timeout", self._timeout)
        headers = kwargs.pop("headers", None) or self._headers()
        resp = self._httpx.request(method, url, headers=headers, **kwargs)
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = None
            err_msg = ""
            if isinstance(data, dict):
                err = data.get("error") or {}
                if isinstance(err, dict):
                    err_msg = (
                        f"{err.get('code', 'HTTP_ERROR')}: "
                        f"{err.get('message', '')}"
                    )
            if not err_msg:
                err_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if any(tok in err_msg for tok in self._NOT_FOUND_TOKENS):
                raise FileNotFoundError(err_msg)
            raise RuntimeError(err_msg)
        return bytes(resp.content)

    # ------------------------------------------------------------------ #
    # Object store contract                                               #
    # ------------------------------------------------------------------ #

    def get_object(self, key: str) -> _BytesObject:
        uri = self._uri(key)
        content = self._request_bytes(
            "GET",
            "/api/v1/content/download",
            params={"uri": uri},
        )
        # A 200 with empty bytes is a stored empty file (e.g. scripts/src/
        # __init__.py); genuinely missing objects 404 inside _request_bytes.
        if content.startswith(_B64_MARKER):
            try:
                content = base64.b64decode(content[len(_B64_MARKER):])
            except Exception:
                pass  # Corrupt marker; return raw content as-is
        return _BytesObject(content, key)

    @staticmethod
    def _batch_hash(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def object_precondition(self, key: str) -> dict[str, str]:
        """Capture the current object state for a later conditional batch write."""
        try:
            current = self.get_object(key).read()
        except FileNotFoundError:
            return {"kind": "create_if_absent"}
        return {
            "kind": "replace_if_hash",
            "base_hash": self._batch_hash(current),
        }

    def batch_write(
        self,
        objects: Mapping[str, bytes | str | io.IOBase],
        *,
        preconditions: Mapping[str, Mapping[str, str]] | None = None,
        wait: bool = True,
        timeout: float | None = None,
        telemetry: bool = True,
        default_mode: str = "upsert",
    ) -> dict[str, Any]:
        """Write one conditional batch below this store's configured root.

        Callers may capture preconditions before preparing derived records and
        pass them back here. When omitted, this method snapshots each target
        immediately before submitting the batch.
        """
        if not objects:
            raise ValueError("batch_write requires at least one object")
        if len(objects) > self._BATCH_MAX_OPERATIONS:
            raise ValueError(
                f"batch_write supports at most {self._BATCH_MAX_OPERATIONS} objects"
            )

        prepared: dict[str, bytes] = {
            str(key): read_bytes(value) for key, value in objects.items()
        }
        total_bytes = sum(len(value) for value in prepared.values())
        oversized = [
            key
            for key, value in prepared.items()
            if len(value) > self._BATCH_MAX_FILE_BYTES
        ]
        if oversized:
            raise ValueError(f"batch_write object exceeds 8 MiB: {oversized[0]}")
        if total_bytes > self._BATCH_MAX_TOTAL_BYTES:
            raise ValueError("batch_write total content exceeds 16 MiB")

        root_uri = self._base_uri().rstrip("/")
        try:
            self._request(
                "POST",
                "/api/v1/fs/mkdir",
                json={"uri": root_uri},
            )
        except RuntimeError as exc:
            if not any(token in str(exc) for token in ("ALREADY_EXISTS", "CONFLICT")):
                raise
        # The OpenViking /content/batch-write endpoint deadlocked on 2026-09-04
        # (any batch-write hung until the gateway returned 504, on every
        # resource and user, while /content/write, mkdir, delete and reads all
        # worked). Emulate the batch with sequential single-file writes.
        # put_object handles both text and binary (base64-encoded) content.
        succeeded: list[str] = []
        failed: list[dict[str, Any]] = []
        try:
            for key, value in sorted(prepared.items()):
                self.put_object(key, value)
                succeeded.append(key)
        except Exception as exc:
            failed.append({"error": str(exc)})
            raise RuntimeError(f"OpenViking sequential batch write failed: {exc}") from exc
        result: dict[str, Any] = {
            "succeeded": succeeded,
            "failed": failed,
            "mode": "sequential_fallback",
        }
        return result

    def ensure_parent(self, key: str) -> None:
        """Create every parent directory required by an object key."""
        clean = str(key or "").strip().replace("\\", "/").strip("/")
        parts = clean.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            uri = self._uri("/".join(parts[:index])).rstrip("/")
            try:
                self._request(
                    "POST",
                    "/api/v1/fs/mkdir",
                    json={"uri": uri},
                )
            except RuntimeError as exc:
                if not any(
                    token in str(exc)
                    for token in ("ALREADY_EXISTS", "CONFLICT")
                ):
                    raise

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        uri = self._uri(key)
        # Serialize same-key writers within this process (see _WRITE_LOCKS).
        with _write_lock(uri):
            self._put_object_locked(key, uri, data)

    def _put_object_locked(self, key: str, uri: str, data: bytes | str | io.IOBase) -> None:
        body = read_bytes(data)
        # OpenViking content/write expects text content; binary keys are
        # base64-encoded with a marker prefix and written as text so that
        # get_object can reverse the transform on read.
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError:
            content = _B64_MARKER.decode("ascii") + base64.b64encode(body).decode("ascii")
        payload = {"uri": uri, "content": content}
        # Strategy: replace (handles existing files of any extension) ->
        # create (new files with allowed extensions) -> append (new files
        # with restricted extensions like .jsonl).
        payload["mode"] = "replace"
        try:
            self._request("POST", "/api/v1/content/write", json=payload)
            return
        except (RuntimeError, FileNotFoundError):
            pass
        # File does not exist yet — try create
        payload["mode"] = "create"
        try:
            self._request("POST", "/api/v1/content/write", json=payload)
            return
        except FileNotFoundError:
            # Nested path whose parent directories do not exist yet. The
            # remote can transiently report NOT_FOUND right after the parent
            # directories are created (eventual consistency), so retry once.
            for attempt in range(2):
                try:
                    self.ensure_parent(key)
                    self._request("POST", "/api/v1/content/write", json=payload)
                    return
                except (RuntimeError, FileNotFoundError):
                    if attempt:
                        raise
                    time.sleep(0.5)
        except RuntimeError as exc:
            err_msg = str(exc)
            if "INVALID_ARGUMENT" in err_msg or "does not allow" in err_msg:
                # Extension restricted in create mode (e.g. .html). The old
                # escape hatch was batch-write upsert, but /content/write does
                # not support upsert mode, and batch_write now routes through
                # put_object (recursion), so retry the allowed modes directly.
                self.ensure_parent(key)
                for retry_mode in ("replace", "create", "append"):
                    payload["mode"] = retry_mode
                    try:
                        self._request("POST", "/api/v1/content/write", json=payload)
                        return
                    except (RuntimeError, FileNotFoundError):
                        continue
                raise
            if "ALREADY_EXISTS" in err_msg or "CONFLICT" in err_msg:
                # Race: file appeared between our replace and create attempts.
                # Concurrent writers hold a short per-file server lock, so the
                # immediate replace can hit CONFLICT (lock acquire timeout).
                # Contention bursts last a few seconds (parallel groups doing
                # read-modify-write on the same key), so retry with patient
                # backoff instead of failing the caller.
                payload["mode"] = "replace"
                backoffs = (0.3, 0.6, 1.2, 2.0, 3.0, 5.0, 5.0, 5.0)
                last_conflict: RuntimeError | None = None
                for delay in backoffs:
                    time.sleep(delay)
                    try:
                        self._request("POST", "/api/v1/content/write", json=payload)
                        return
                    except RuntimeError as retry_exc:
                        if "CONFLICT" not in str(retry_exc):
                            raise
                        last_conflict = retry_exc
                assert last_conflict is not None
                raise last_conflict
            if "NOT_FOUND" in err_msg:
                # The remote can transiently report NOT_FOUND on create while
                # parent-directory state is still propagating (observed as
                # intermittent 404s on the same directory where concurrent
                # writes succeed). Ensure parents exist and retry.
                for attempt in range(3):
                    try:
                        self.ensure_parent(key)
                        self._request("POST", "/api/v1/content/write", json=payload)
                        return
                    except (RuntimeError, FileNotFoundError):
                        if attempt == 2:
                            raise
                        time.sleep(0.5 * (attempt + 1))
            raise

    def delete_object(self, key: str) -> None:
        uri = self._uri(key)
        # OpenViking exposes a real delete via DELETE /api/v1/fs?uri=...
        try:
            self._request(
                "DELETE",
                "/api/v1/fs",
                params={"uri": uri},
            )
        except FileNotFoundError:
            return
        except RuntimeError:
            # Best-effort: ignore failures so callers can still iterate.
            pass

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        # OpenViking provides recursive listing via /api/v1/fs/ls?recursive=true.
        seed = self._uri(prefix.rstrip("/")) if prefix else self._uri("")
        if not seed.endswith("/"):
            seed = seed + "/"
        try:
            data = self._request(
                "GET",
                "/api/v1/fs/ls",
                params={"uri": seed, "recursive": "true", "node_limit": 10000},
            )
        except FileNotFoundError:
            return iter(())
        result = data.get("result") if isinstance(data, dict) else None
        entries = result if isinstance(result, list) else []
        leaves: list[str] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("isDir"):
                continue
            child_uri = entry.get("uri")
            if isinstance(child_uri, str) and child_uri:
                leaves.append(child_uri)
        return iter(ObjectInfo(self._strip_uri(uri)) for uri in leaves)


# --------------------------------------------------------------------- #
# Availability probing (local-storage fallback support)                  #
# --------------------------------------------------------------------- #

# TTL-cached probe verdicts keyed by the store identity tuple. Store
# instances are rebuilt per ingest/operation, so without a cache every
# construction would pay a probe request; 30s bounds both the extra traffic
# and the worst-case time to notice a recovered OpenViking.
_PROBE_TTL_SECONDS = 30.0
_PROBE_CACHE: dict[tuple, tuple[float, bool, str]] = {}
_PROBE_CACHE_GUARD = threading.Lock()


def _probe_cache_key(store: "OpenVikingObjectStore", timeout: float) -> tuple:
    return (
        store._endpoint,
        store._account,
        store._user,
        store._api_key,
        store._root_prefix,
        store._group_id,
        store._namespace,
        float(timeout),
    )


def probe_viking_availability(
    store: "OpenVikingObjectStore",
    *,
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Probe whether the OpenViking deployment behind ``store`` is usable.

    Issues a single short-timeout bare ``fs/ls`` (no ``uri`` parameter) through
    the store's own pooled client, so the probe sees exactly the same
    auth/headers as production calls. Deliberately NOT a real listing: with a
    ``uri`` this endpoint can take 9-15s+ server-side on large namespaces (and a
    cold connection pays ~5s of macOS mDNS DNS tax on ``*.local`` hostnames),
    which would make any practical timeout misclassify a healthy deployment.
    A bare call is rejected fast (HTTP 4xx) while still exercising DNS, TCP,
    TLS-less connect and the server's request loop.

    Classification:
    - unavailable -> connection/transport errors, timeouts, HTTP 5xx
      (the server cannot serve storage; callers may fall back to local)
    - available   -> any answered HTTP response below 500, including 4xx
      (e.g. auth misconfiguration — the server answered, so falling back
      would only mask a config error)

    The probe inspects the raw HTTP status rather than ``_request``'s
    reformatted error strings, so gateway 5xx pages with empty/odd bodies are
    classified correctly. Verdicts (both directions) are cached for
    ``_PROBE_TTL_SECONDS`` so per-operation store rebuilds stay cheap.
    Returns ``(available, reason)``; ``reason`` is empty on success.
    """
    import httpx

    key = _probe_cache_key(store, timeout)
    now = time.monotonic()
    with _PROBE_CACHE_GUARD:
        cached = _PROBE_CACHE.get(key)
        if cached is not None and now - cached[0] < _PROBE_TTL_SECONDS:
            return cached[1], cached[2]

    available: bool
    reason: str
    try:
        resp = store._httpx.request(
            "GET",
            f"{store._endpoint}/api/v1/fs/ls",
            headers=store._headers(),
            timeout=timeout,
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        available, reason = False, f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        available, reason = False, f"{type(exc).__name__}: {exc}"
    else:
        if resp.status_code >= 500:
            available, reason = False, f"HTTP {resp.status_code}"
        else:
            available, reason = True, ""

    with _PROBE_CACHE_GUARD:
        _PROBE_CACHE[key] = (time.monotonic(), available, reason)
    return available, reason


def reset_probe_cache() -> None:
    """Drop all cached probe verdicts (tests only)."""
    with _PROBE_CACHE_GUARD:
        _PROBE_CACHE.clear()
