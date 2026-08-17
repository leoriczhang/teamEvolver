"""OpenViking-backed object store."""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Any, Iterator, Mapping

from .base import ObjectInfo, _BytesObject, read_bytes

# Wire constant, do not rename. This string is a shared data contract: Hermes'
# AgentsHub and the evolve server read team skills from this shared contract.
_VIKING_ROOT_PREFIX = "team-skill-evolver"


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
        self._httpx = httpx

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
        if not content:
            raise FileNotFoundError(f"OpenViking: empty/missing object: {uri}")
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

        captured = {
            key: dict((preconditions or {}).get(key) or self.object_precondition(key))
            for key in prepared
        }
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
        operations: list[dict[str, Any]] = []
        for key, value in sorted(prepared.items()):
            operation: dict[str, Any] = {
                "uri": self._uri(key),
                "precondition": captured[key],
            }
            try:
                operation["content"] = value.decode("utf-8")
            except UnicodeDecodeError:
                operation["content_base64"] = base64.b64encode(value).decode("ascii")
            operations.append(operation)

        payload: dict[str, Any] = {
            "root_uri": root_uri,
            "operations": operations,
            "wait": bool(wait),
            "telemetry": bool(telemetry),
        }
        if timeout is not None:
            payload["timeout"] = float(timeout)
        response = self._request(
            "POST",
            "/api/v1/content/batch-write",
            json=payload,
        )
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("OpenViking batch-write returned no result")
        if response.get("telemetry") is not None:
            result = {**result, "telemetry": response["telemetry"]}
        return result

    def put_object(self, key: str, data: bytes | str | io.IOBase) -> None:
        uri = self._uri(key)
        body = read_bytes(data)
        # OpenViking content/write expects text content; binary keys are
        # stored base64-encoded.  We probe by trying utf-8 first.
        try:
            content = body.decode("utf-8")
            payload = {"uri": uri, "content": content}
        except UnicodeDecodeError:
            self.batch_write({key: body})
            return
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
        except RuntimeError as exc:
            err_msg = str(exc)
            if "INVALID_ARGUMENT" in err_msg or "does not allow" in err_msg:
                # Extension restricted in create mode; append creates the file
                payload["mode"] = "append"
                self._request("POST", "/api/v1/content/write", json=payload)
                return
            if "ALREADY_EXISTS" in err_msg or "CONFLICT" in err_msg:
                # Race: file appeared between our replace and create attempts
                payload["mode"] = "replace"
                self._request("POST", "/api/v1/content/write", json=payload)
                return
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
