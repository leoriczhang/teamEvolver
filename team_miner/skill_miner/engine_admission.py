"""Durable Native Compile reservations; unknown remote outcomes never expire."""

from __future__ import annotations

import json

from teamEvolver.storage.postgres import PostgresObjectStore

from .compile_admission import MAX_NATIVE_COMPILES, OPTIMIZED_PROTOCOL, TERMINAL_NATIVE_STATES


class CompileAdmission:
    def __init__(self, services, tenant, task=None):
        self.services, self.tenant, self.task = services, tenant, task
        self.store = PostgresObjectStore(services.tx, tenant)
        self.key = "skill-mining/admission/native.json"

    async def _load(self, conn):
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           self.tenant + ":skill-mining:compile-admission")
        if self.task is not None:
            await self.services.tasks.require_current(conn, self.task, allow_cancel=True)
        try:
            value = json.loads(await self.store.get(conn, self.key))
        except FileNotFoundError:
            return {"version": 1, "reservations": {}}
        if value.get("version") != 1 or not isinstance(value.get("reservations"), dict):
            raise ValueError("Invalid durable Compile admission journal")
        return value

    async def _save(self, conn, value):
        await self.store.put(conn, self.key, json.dumps(value, ensure_ascii=False, sort_keys=True))

    async def acquire(self, job, current):
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            value = await self._load(conn)
            reservations, key = value["reservations"], current["key"]
            if key in reservations:
                row = reservations[key]
                if row["job_id"] != job["job_id"] or row["round"] != int(job["current_round"]):
                    raise ValueError("Compile reservation belongs to another job")
                return True
            live = {"key:" + identity for identity in reservations}
            covered_tasks = {row.get("task_id") for row in reservations.values() if row.get("task_id")}
            covered_jobs = {row["job_id"] for row in reservations.values()}
            rows = await conn.fetch(
                f"SELECT document FROM {self.services.tx.pool.schema}.skill_mining_jobs "
                "WHERE tenant_id=$1 AND NOT deleted", self.tenant,
            )
            for stored in rows:
                other = stored["document"]
                other = json.loads(other) if isinstance(other, str) else other
                task_id = other.get("ov_task_id")
                if (other.get("engine") == "ov_compile" and task_id
                        and (other.get("status") in {"queued", "running", "preparing", "stopping"}
                             or other.get("remote_terminal_unconfirmed"))
                        and other["job_id"] not in covered_jobs and task_id not in covered_tasks):
                    live.add("task:" + str(task_id))
            if len(live) >= MAX_NATIVE_COMPILES:
                return False
            reservations[key] = {"job_id": job["job_id"], "round": int(job["current_round"]),
                                 "account": str(job.get("account") or ""), "task_id": ""}
            await self._save(conn, value)
            return True

    async def reservations(self):
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            value = await self._load(conn)
        return [{"key": key, **row} for key, row in value["reservations"].items()]

    async def mark(self, current):
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            value = await self._load(conn)
            row = value["reservations"].get(current["key"])
            if row:
                if row.get("task_id") and row["task_id"] != current["task_id"]:
                    raise ValueError("Idempotent Compile reservation changed task identity")
                row["task_id"] = current["task_id"]
                await self._save(conn, value)

    async def release(self, *, task_id="", key=""):
        if not task_id and not key:
            return
        async with self.services.tx.tenant_tx(self.tenant) as conn:
            value = await self._load(conn)
            for identity, row in list(value["reservations"].items()):
                if identity == key or (task_id and row.get("task_id") == task_id):
                    value["reservations"].pop(identity)
            await self._save(conn, value)


class MiningAdmissionMixin:
    async def _acquire_staged_compile_slot(self, job, state, current):
        if state.get("execution_protocol") != OPTIMIZED_PROTOCOL:
            return True
        if await self.admission.acquire(job, current):
            return True
        for row in await self.admission.reservations():
            if not row.get("task_id"):
                try:
                    accepted = await self._knowledge_request(
                        row["account"], "GET", f"/api/v1/compile/submissions/{row['key']}", timeout=5)
                except Exception:
                    continue
                if not isinstance(accepted, dict) or not accepted.get("task_id"):
                    continue
                row["task_id"] = str(accepted["task_id"])
                await self.admission.mark(row)
            try:
                task = await self._knowledge_request(
                    row["account"], "GET", f"/api/v1/tasks/{row['task_id']}", timeout=5)
            except Exception:
                continue
            if isinstance(task, dict) and task.get("status") in TERMINAL_NATIVE_STATES:
                await self.admission.release(task_id=row["task_id"])
        return await self.admission.acquire(job, current)

    async def _mark_staged_compile_slot(self, job, current):
        await self.admission.mark(current)

    async def _release_staged_compile_slot(self, job, task_id="", *, key=""):
        await self.admission.release(task_id=task_id, key=key)
