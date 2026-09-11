"""Contract tests for the built-in local filesystem object store.

``LocalObjectStore`` is teamEvolver's own storage backend — the automatic
fallback when the configured OpenViking deployment is unavailable. These tests
pin the object-store contract (parity with ``InMemoryObjectStore`` semantics)
plus the local-specific hardening: atomic writes, path-traversal guards, and
thread safety.
"""

from __future__ import annotations

import threading

import pytest

from teamEvolver.storage import LocalObjectStore, is_not_found_error


def test_put_get_roundtrip(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("skills/demo/SKILL.md", b"hello")
    assert store.get_object("skills/demo/SKILL.md").read() == b"hello"


def test_put_accepts_str_and_stream(tmp_path) -> None:
    import io

    store = LocalObjectStore(tmp_path)
    store.put_object("a.txt", "text")
    store.put_object("b.txt", io.BytesIO(b"bytes"))
    assert store.get_object("a.txt").read() == b"text"
    assert store.get_object("b.txt").read() == b"bytes"


def test_get_missing_raises_not_found(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get_object("nope.json")
    # Contract parity: the shared helper must classify it as not-found.
    try:
        store.get_object("nope.json")
    except Exception as exc:  # noqa: BLE001
        assert is_not_found_error(exc)


def test_put_overwrites_existing(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("k", b"v1")
    store.put_object("k", b"v2")
    assert store.get_object("k").read() == b"v2"


def test_binary_payloads_survive(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    payload = bytes(range(256))
    store.put_object("bin/data.bin", payload)
    assert store.get_object("bin/data.bin").read() == payload


def test_delete_is_idempotent(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("x.json", b"{}")
    store.delete_object("x.json")
    store.delete_object("x.json")  # second delete must not raise
    with pytest.raises(FileNotFoundError):
        store.get_object("x.json")


def test_iter_objects_prefix_filtering_and_order(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("sessions/b.json", b"2")
    store.put_object("sessions/a.json", b"1")
    store.put_object("manifest.json", b"{}")
    store.put_object("skills/demo/SKILL.md", b"x")
    keys = [obj.key for obj in store.iter_objects(prefix="sessions/")]
    assert keys == ["sessions/a.json", "sessions/b.json"]
    all_keys = [obj.key for obj in store.iter_objects()]
    assert all_keys == sorted(all_keys)
    assert "manifest.json" in all_keys


def test_iter_objects_normalizes_backslash_prefix(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("sessions/a.json", b"1")
    keys = [obj.key for obj in store.iter_objects(prefix="sessions\\")]
    assert keys == ["sessions/a.json"]


def test_iter_objects_missing_root_returns_empty(tmp_path) -> None:
    store = LocalObjectStore(tmp_path / "not-created-yet")
    assert list(store.iter_objects()) == []


def test_rejects_path_traversal_keys(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    for bad in ("../escape", "a/../../escape"):
        with pytest.raises(ValueError):
            store.put_object(bad, b"x")
        with pytest.raises(ValueError):
            store.get_object(bad)
    with pytest.raises(ValueError):
        store.put_object("", b"x")


def test_absolute_style_keys_are_normalized_like_other_stores(tmp_path) -> None:
    # Contract parity with InMemoryObjectStore/OpenVikingObjectStore: leading
    # slashes are stripped rather than rejected.
    store = LocalObjectStore(tmp_path)
    store.put_object("/abs/path", b"v")
    assert store.get_object("abs/path").read() == b"v"
    assert (tmp_path / "abs" / "path").is_file()


def test_atomic_write_leaves_no_tmp_residue(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    store.put_object("dir/k.json", b"{}")
    leftovers = [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]
    assert leftovers == []
    keys = [obj.key for obj in store.iter_objects()]
    assert keys == ["dir/k.json"]


def test_concurrent_writes_smoke(tmp_path) -> None:
    store = LocalObjectStore(tmp_path)
    errors: list[Exception] = []

    def write_many(tag: int) -> None:
        try:
            for i in range(50):
                store.put_object(f"t{tag}/{i}.json", f"{tag}:{i}".encode())
                store.put_object("shared/hot.json", f"{tag}:{i}".encode())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write_many, args=(t,)) for t in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(list(store.iter_objects(prefix="t0/"))) == 50
    assert len(list(store.iter_objects(prefix="shared/"))) == 1


def test_no_native_batch_write_attribute(tmp_path) -> None:
    # Batch callers degrade to per-object writes when this is absent; defining
    # it would route binary writes through the viking-only batch contract.
    store = LocalObjectStore(tmp_path)
    assert getattr(store, "native_batch_write", False) is False
    assert getattr(store, "batch_write", None) is None
