"""Fail-open Langfuse tracing for teamEvolver model and agent workflows."""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

logger = logging.getLogger(__name__)

_REDACTED = {"redacted": True}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    try:
        value = max(minimum, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default
    return min(value, maximum) if maximum is not None else value


def _normalize_environment(value: Any) -> str:
    normalized = str(value or "local").strip().lower().replace(" ", "-")
    if not normalized or normalized.startswith("langfuse"):
        return "local"
    return "".join(
        char for char in normalized if char.isalnum() or char in {"-", "_"}
    ) or "local"


@dataclass(frozen=True)
class LangfuseTracingSettings:
    enabled: bool = False
    host: str = "http://127.0.0.1:3000"
    public_key: str = ""
    secret_key: str = ""
    environment: str = "local"
    release: str = ""
    sample_rate: float = 1.0
    capture_content: bool = True
    flush_at: int = 1
    flush_interval_seconds: float = 1.0
    timeout_seconds: int = 30

    @classmethod
    def from_config(cls, config: Any | None) -> "LangfuseTracingSettings":
        get = lambda name, default: getattr(config, name, default) if config is not None else default
        configured_enabled = bool(get("langfuse_tracing_enabled", False))
        enabled = _env_bool("LANGFUSE_TRACING_ENABLED", configured_enabled)
        host = str(
            os.environ.get("LANGFUSE_BASE_URL")
            or os.environ.get("LANGFUSE_HOST")
            or get("langfuse_host", "")
            or "http://127.0.0.1:3000"
        ).strip().rstrip("/")
        public_key = str(
            os.environ.get("LANGFUSE_PUBLIC_KEY")
            or get("langfuse_public_key", "")
            or ""
        ).strip()
        secret_key = str(
            os.environ.get("LANGFUSE_SECRET_KEY")
            or get("langfuse_secret_key", "")
            or ""
        ).strip()
        environment = _normalize_environment(
            os.environ.get("LANGFUSE_TRACING_ENVIRONMENT")
            or get("langfuse_tracing_environment", "local")
        )
        release = str(
            os.environ.get("LANGFUSE_RELEASE")
            or get("langfuse_tracing_release", "")
            or ""
        ).strip()
        sample_rate = _env_float(
            "LANGFUSE_SAMPLE_RATE",
            float(get("langfuse_tracing_sample_rate", 1.0)),
            maximum=1.0,
        )
        capture_content = _env_bool(
            "LANGFUSE_CAPTURE_CONTENT",
            bool(get("langfuse_tracing_capture_content", True)),
        )
        flush_at = _env_int(
            "LANGFUSE_FLUSH_AT",
            int(get("langfuse_tracing_flush_at", 1) or 1),
        )
        flush_interval = _env_float(
            "LANGFUSE_FLUSH_INTERVAL",
            float(
                get("langfuse_tracing_flush_interval_seconds", 1.0) or 1.0
            ),
            minimum=0.1,
        )
        timeout_seconds = _env_int(
            "LANGFUSE_TIMEOUT",
            int(get("langfuse_timeout_seconds", 30) or 30),
        )
        return cls(
            enabled=enabled,
            host=host,
            public_key=public_key,
            secret_key=secret_key,
            environment=environment,
            release=release,
            sample_rate=sample_rate,
            capture_content=capture_content,
            flush_at=flush_at,
            flush_interval_seconds=flush_interval,
            timeout_seconds=timeout_seconds,
        )


class _LangfuseRuntime:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings = LangfuseTracingSettings()
        self._client: Any | None = None
        self._last_error = ""

    def configure(self, config: Any | None = None) -> dict[str, Any]:
        settings = LangfuseTracingSettings.from_config(config)
        with self._lock:
            if settings != self._settings:
                self._shutdown_locked()
                self._settings = settings
                self._last_error = ""
        return self.status()

    def _shutdown_locked(self) -> None:
        if self._client is None:
            return
        try:
            # Langfuse clients are cached by public key. Resetting the resource
            # manager is required for same-key host/config hot reloads.
            from langfuse._client.resource_manager import LangfuseResourceManager

            LangfuseResourceManager.reset()
        except Exception:
            try:
                self._client.shutdown()
            except Exception:
                logger.debug("[Langfuse] shutdown failed", exc_info=True)
        finally:
            self._client = None

    def _ensure_client(self) -> Any | None:
        with self._lock:
            settings = self._settings
            if not settings.enabled:
                return None
            if not settings.public_key or not settings.secret_key:
                self._last_error = "Langfuse tracing requires public_key and secret_key"
                return None
            if self._client is not None:
                return self._client
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=settings.public_key,
                    secret_key=settings.secret_key,
                    base_url=settings.host,
                    timeout=settings.timeout_seconds,
                    tracing_enabled=True,
                    flush_at=settings.flush_at,
                    flush_interval=settings.flush_interval_seconds,
                    environment=settings.environment,
                    release=settings.release or None,
                    sample_rate=settings.sample_rate,
                )
                self._last_error = ""
                logger.info(
                    "[Langfuse] tracing enabled: host=%s environment=%s sample_rate=%.3f",
                    settings.host,
                    settings.environment,
                    settings.sample_rate,
                )
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[Langfuse] tracing unavailable: %s",
                    self._last_error,
                )
                self._client = None
            return self._client

    def capture(self, value: Any) -> Any:
        if value is None or self._settings.capture_content:
            return value
        return dict(_REDACTED)

    @contextmanager
    def observation(
        self,
        *,
        name: str,
        as_type: str = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str = "",
        model_parameters: dict[str, Any] | None = None,
        trace_name: str = "",
        session_id: str = "",
        user_id: str = "",
        tags: list[str] | None = None,
    ) -> Iterator[Any | None]:
        client = self._ensure_client()
        if client is None:
            yield None
            return

        stack = ExitStack()
        observation = None
        try:
            from langfuse import propagate_attributes

            trace_tags = list(
                dict.fromkeys(
                    tag
                    for raw in ["teamEvolver", *(tags or [])]
                    if (tag := str(raw or "").strip())
                )
            )
            stack.enter_context(
                propagate_attributes(
                    trace_name=trace_name or name,
                    session_id=session_id or None,
                    user_id=user_id or None,
                    tags=trace_tags,
                    metadata=metadata or None,
                )
            )
            observation = stack.enter_context(
                client.start_as_current_observation(
                    as_type=as_type,
                    name=name,
                    input=self.capture(input),
                    metadata=metadata or None,
                    model=model or None,
                    model_parameters=model_parameters or None,
                )
            )
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("[Langfuse] failed to start observation: %s", exc)
            try:
                stack.close()
            except Exception:
                pass
            yield None
            return

        try:
            yield observation
        except BaseException as exc:
            self.update(
                observation,
                level="ERROR",
                status_message=f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            try:
                stack.close()
            except Exception:
                logger.debug(
                    "[Langfuse] failed to close observation",
                    exc_info=True,
                )

    def update(
        self,
        observation: Any | None,
        *,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
        usage_details: dict[str, int] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        if observation is None:
            return
        payload: dict[str, Any] = {}
        if output is not None:
            payload["output"] = self.capture(output)
        if metadata:
            payload["metadata"] = metadata
        if usage_details:
            payload["usage_details"] = usage_details
        if level:
            payload["level"] = level
        if status_message:
            payload["status_message"] = status_message
        if not payload:
            return
        try:
            observation.update(**payload)
        except Exception:
            logger.debug("[Langfuse] observation update failed", exc_info=True)

    def flush(self) -> None:
        with self._lock:
            if self._client is None:
                return
            try:
                self._client.flush()
            except Exception:
                logger.warning("[Langfuse] flush failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        with self._lock:
            settings = self._settings
            return {
                "enabled": settings.enabled,
                "sdk_available": importlib.util.find_spec("langfuse") is not None,
                "initialized": self._client is not None,
                "host": settings.host,
                "environment": settings.environment,
                "release": settings.release,
                "sample_rate": settings.sample_rate,
                "capture_content": settings.capture_content,
                "last_error": self._last_error,
            }


_RUNTIME = _LangfuseRuntime()


def configure_langfuse(config: Any | None = None) -> dict[str, Any]:
    return _RUNTIME.configure(config)


def langfuse_observation(**kwargs: Any):
    return _RUNTIME.observation(**kwargs)


def update_langfuse_observation(observation: Any | None, **kwargs: Any) -> None:
    _RUNTIME.update(observation, **kwargs)


def flush_langfuse() -> None:
    _RUNTIME.flush()


def langfuse_status() -> dict[str, Any]:
    return _RUNTIME.status()
