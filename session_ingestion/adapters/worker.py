"""Build an immutable adapter revision and stream bounded batches on Worker."""
from __future__ import annotations

import asyncio

from ._runtime import apply_session_id_exclusions, load_from_content


class AdapterSource:
    def load(self, *, adapter_id, revision, source):
        return StreamingAdapter(source, adapter_id)


class StreamingAdapter:
    def __init__(self, source, filename):
        self.source, self.filename = source, filename

    async def iter_sessions(self, filters):
        adapter, metadata, _ = await asyncio.to_thread(load_from_content, self.source, self.filename)
        def next_batch(iterator):
            return next(iterator, None)
        try:
            iterator = iter(adapter.iter_session_id_batches(filters, batch_size=100))
            while True:
                batch = await asyncio.to_thread(next_batch, iterator)
                if batch is None:
                    break
                if not isinstance(batch, (list, tuple)) or len(batch) > 1000:
                    raise ValueError("adapter batch must contain at most 1000 Session IDs")
                ids, _ = apply_session_id_exclusions(metadata, filters, list(dict.fromkeys(batch)))
                semaphore = asyncio.Semaphore(8)
                async def fetch(sid):
                    async with semaphore:
                        try:
                            raw, traces = await asyncio.to_thread(adapter.fetch_session, sid)
                            session = await asyncio.to_thread(adapter.convert_session, raw, traces)
                            return {"external_id": sid, "session": session}
                        except Exception:
                            return {"external_id": sid, "status": "error"}
                for item in await asyncio.gather(*(fetch(sid) for sid in ids)):
                    yield item
        finally:
            await asyncio.to_thread(adapter.close)
