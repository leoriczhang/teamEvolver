"""Multi-process fault verification: 2 API + 2 Worker + 1 PostgreSQL.

Every process is a real ``teamEvolver start --role`` of the *installed* release
(no source tree on ``sys.path``). A fault-injectable OpenViking stand-in
(``scripts/fake_openviking.py``) serves Compile; a round-robin load balancer
with no session affinity alternates requests across both API processes.

Scenarios:

1. no-sticky LB: login on one API, every subsequent request alternates; all
   responses agree, including the job enqueued on one API and read on the other.
2. Worker crash: SIGKILL the Worker holding a Compile task; after lease expiry
   the other Worker reclaims it, resumes the same upstream task (no resubmit),
   the fence advances and the watermark is written once.
3. PG short outage: the PG container is paused; API returns errors and readiness
   behaviour is recorded, Workers keep running; after resume a new task completes.
4. OpenViking short outage: status polling fails with 503 while a task runs;
   the task is not finalized as failed; after recovery it succeeds once.
5. Late result: an attempt fenced out by a reclaim cannot overwrite the newer
   attempt (repository-level write with the stale fence is rejected).
6. Unknown submit outcome: the Compile submit response is dropped; the task
   reconciles by Idempotency-Key and finishes with exactly one upstream task.

    python scripts/verify_cluster_faults.py --python <venv>/bin/python \
        --dsn postgresql://app:...@127.0.0.1:55432/test --pg-container isolated-test-pg \
        --evidence /tmp/te-fault-evidence
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
ROOT_KEY = "fault-test-root-key"
ADMIN = {"username": "root", "password": "fault-test-password"}
SCHEMA = f"te_fault_{os.getpid()}_{time.time_ns()}"
LEASE_SECONDS = 60  # worker default (teamEvolver/runtime/worker.py)


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #


class Cluster:
    def __init__(self, args, workdir: Path):
        self.args, self.workdir = args, workdir
        self.procs: dict[str, subprocess.Popen] = {}
        self.ports = {"api-a": 56110, "api-b": 56111, "worker-a": 56120, "worker-b": 56121, "ov": 56190, "lb": 56100}
        self.dsn = args.dsn

    def env(self, name):
        home = self.workdir / name
        home.mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}}
        env.update(
            {
                "HOME": str(home),
                "TEAMEVOLVER_PG_DSN": self.dsn,
                "TEAMEVOLVER_PG_SCHEMA": SCHEMA,
                "TEAMEVOLVER_ROOT_API_KEY": ROOT_KEY,
                "TEAMEVOLVER_ENV": "cluster",
                "TEAMEVOLVER_OV_ENDPOINT": f"http://127.0.0.1:{self.ports['ov']}",
                "TEAMEVOLVER_OV_API_KEY": "fake-ov-root",
                "TEAMEVOLVER_API_HOST": "127.0.0.1",
            }
        )
        return env

    def start(self, name):
        log = (self.workdir / f"{name}.log").open("ab")
        if name == "ov":
            argv = [self.args.python, str(ROOT / "scripts/fake_openviking.py"), "--port", str(self.ports["ov"])]
        else:
            role = name.split("-")[0]
            argv = [self.args.python, "-m", "teamEvolver", "start", "--role", role, "--port", str(self.ports[name])]
        # Run outside the source tree so only the installed release is importable.
        self.procs[name] = subprocess.Popen(
            argv, cwd=self.workdir, env=self.env(name), stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        (self.workdir / f"{name}.pid").write_text(str(self.procs[name].pid))
        if name != "ov":
            wait_until(lambda: http("GET", self.url(name, "/readyz"))[0] == 200, 60, f"{name} ready")

    def url(self, name, path):
        return f"http://127.0.0.1:{self.ports[name]}{path}"

    def kill(self, name, sig=signal.SIGKILL):
        proc = self.procs.pop(name)
        proc.send_signal(sig)
        code = proc.wait(timeout=60)
        (self.workdir / f"{name}.pid").unlink(missing_ok=True)
        return code

    def stop_all(self):
        codes = {}
        for name in list(self.procs):
            codes[name] = self.kill(name, signal.SIGTERM)
        return codes


class RoundRobin:
    """Layer-7 proxy that alternates upstreams per request (no affinity)."""

    def __init__(self, port, upstreams):
        cycle, hits, lock = itertools.cycle(upstreams), {u: 0 for u in upstreams}, threading.Lock()
        self.hits = hits

        class Proxy(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                return

            def _proxy(self):
                with lock:
                    upstream = next(cycle)
                    hits[upstream] += 1
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in {"host", "content-length"}}
                headers["Host"] = f"127.0.0.1:{port}"  # same cookie scope for both upstreams
                request = urllib.request.Request(upstream + self.path, data=body, method=self.command, headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=90) as response:
                        status, out, rh = response.status, response.read(), response.headers
                except urllib.error.HTTPError as exc:
                    status, out, rh = exc.code, exc.read(), exc.headers
                except OSError:
                    status, out, rh = 502, b'{"detail":"upstream unavailable"}', {}
                self.send_response(status)
                for key, value in rh.items() if rh else []:
                    if key.lower() not in {"transfer-encoding", "connection", "content-length", "server", "date"}:
                        self.send_header(key, value)
                self.send_header("X-Upstream", upstream.rsplit(":", 1)[1])
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_DELETE = _proxy

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Proxy)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def http(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()), response.headers
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw.decode()), exc.headers
    except OSError as exc:
        return 0, str(exc), {}


def wait_until(check, timeout, label, interval=0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except Exception:  # noqa: BLE001 - retried until the deadline
            pass
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {label}")


def ov(cluster, path, body=None):
    return http("POST" if body is not None else "GET", cluster.url("ov", path), body)[1]


class DB:
    def __init__(self, dsn):
        self.dsn = dsn

    def fetch(self, sql, *args, tenant="default"):
        async def run():
            conn = await asyncpg.connect(self.dsn)
            try:
                await conn.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
                return [dict(r) for r in await conn.fetch(sql, *args)]
            finally:
                await conn.close()

        return asyncio.run(run())

    def task(self, task_id):
        rows = self.fetch(
            f"SELECT status, attempt, fence, owner_id, upstream_task_id, error_code "
            f"FROM {SCHEMA}.worker_tasks WHERE task_id=$1",
            task_id,
        )
        return rows[0] if rows else None


def miner_state(db, kind, identity):
    """Read a Team Miner ObjectStore record through the installed release."""
    from team_miner.state import MinerState

    async def run():
        from teamEvolver.database import get_database_pool
        from teamEvolver.database.transaction import TransactionManager

        pool = get_database_pool(dsn=db.dsn, schema=SCHEMA, ssl="disable")
        tx = TransactionManager(pool)
        async with tx.tenant_tx("default") as conn:
            return await MinerState(tx, "default").get(conn, kind, identity)

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #


class Session:
    """Console session through the LB; the cookie is the only shared state."""

    def __init__(self, base):
        self.base, self.cookie = base, ""
        self.upstreams: list[str] = []

    def call(self, method, path, body=None, extra=None, timeout=30):
        headers = {"X-Tenant-Id": "default", **(extra or {})}
        if self.cookie:
            headers["Cookie"] = self.cookie
        status, data, rh = http(method, self.base + path, body, headers, timeout=timeout)
        if rh and rh.get("Set-Cookie"):
            self.cookie = rh["Set-Cookie"].split(";", 1)[0]
        if rh and rh.get("X-Upstream"):
            self.upstreams.append(rh["X-Upstream"])
        return status, data


def compile_task(session, key):
    status, data = session.call("POST", "/api/knowledge-mining/tasks", {}, {"Idempotency-Key": key})
    assert status == 202, (status, data)
    return data["job_id"]


def scenario_no_sticky(cluster, session, db, report):
    status, data = session.call("POST", "/api/auth/bootstrap", ADMIN)
    assert status == 200, (status, data)
    views = [session.call("GET", "/api/auth/status")[1]["user"]["id"] for _ in range(6)]
    configs = [session.call("GET", "/api/knowledge-mining/config")[1]["revision"] for _ in range(4)]
    job = compile_task(session, "fault-no-sticky-0001")
    reads = [session.call("GET", f"/api/jobs/{job}") for _ in range(4)]
    assert set(views) == {"root"} and len(set(configs)) == 1
    assert all(s == 200 and r["job_id"] == job for s, r in reads), reads
    served = sorted(set(session.upstreams))
    assert served == sorted(str(cluster.ports[n]) for n in ("api-a", "api-b")), served
    wait_until(lambda: db.task(job)["upstream_task_id"], 30, "upstream submit")
    ov(cluster, "/_ctl/finish", {"task_id": db.task(job)["upstream_task_id"]})
    wait_until(lambda: db.task(job)["status"] == "succeeded", 30, "no-sticky job succeeded")
    report["no_sticky"] = {
        "requests": len(session.upstreams),
        "upstreams": served,
        "user_views": sorted(set(views)),
        "job": db.task(job),
    }


def scenario_worker_crash(cluster, session, db, report):
    job = compile_task(session, "fault-worker-crash-0001")
    wait_until(lambda: db.task(job)["upstream_task_id"], 30, "first attempt submitted")
    first = db.task(job)
    owner = next(n for n in ("worker-a", "worker-b") if str(cluster.procs[n].pid) in first["owner_id"])
    survivor = "worker-b" if owner == "worker-a" else "worker-a"
    code = cluster.kill(owner)
    submits_before = len(ov(cluster, "/_ctl/state")["submissions"])
    # Expire the dead attempt's lease instead of waiting the full 60 s window.
    db.fetch(f"UPDATE {SCHEMA}.worker_tasks SET lease_until=now() WHERE task_id=$1 RETURNING task_id", job)
    wait_until(lambda: db.task(job)["fence"] > first["fence"], 30, "survivor reclaimed")
    reclaimed = db.task(job)
    ov(cluster, "/_ctl/finish", {"task_id": first["upstream_task_id"]})
    wait_until(lambda: db.task(job)["status"] == "succeeded", 30, "reclaimed task succeeded")
    final = db.task(job)
    state = ov(cluster, "/_ctl/state")
    receipt = miner_state(db, "receipt", job)
    assert str(cluster.procs[survivor].pid) in reclaimed["owner_id"]
    assert final["upstream_task_id"] == first["upstream_task_id"]
    assert len(state["submissions"]) == submits_before, "reclaim must not resubmit"
    assert receipt["finalized"] and receipt["status"] == "succeeded"
    report["worker_crash"] = {
        "killed": owner,
        "kill_exit": code,
        "first": first,
        "reclaimed": reclaimed,
        "final": final,
        "upstream_submissions": len(state["submissions"]),
    }
    cluster.start(owner)


def scenario_late_result(cluster, session, db, report):
    from teamEvolver.database import get_database_pool
    from teamEvolver.database.transaction import TransactionManager
    from teamEvolver.runtime.repository import WorkerTaskRepository

    job = report["worker_crash"]["first"]
    upstream = report["worker_crash"]["final"]["upstream_task_id"]
    task_id = db.fetch(
        f"SELECT task_id FROM {SCHEMA}.worker_tasks WHERE upstream_task_id=$1 AND kind='miner.track_compile'", upstream
    )[0]["task_id"]

    async def stale_write():
        pool = get_database_pool(dsn=db.dsn, schema=SCHEMA, ssl="disable")
        tx = TransactionManager(pool)
        async with tx.tenant_tx("default") as conn:
            repo = WorkerTaskRepository(tx)
            return await repo.fail(
                conn,
                task_id=task_id,
                owner_id=job["owner_id"],
                fence=job["fence"],
                error_code="late_result_from_dead_attempt",
            )

    accepted = asyncio.run(stale_write())
    after = db.task(task_id)
    assert accepted is False and after["status"] == "succeeded", (accepted, after)
    report["late_result"] = {
        "stale_fence": job["fence"],
        "current_fence": after["fence"],
        "write_accepted": accepted,
        "status_after": after["status"],
    }


def scenario_openviking_outage(cluster, session, db, report):
    job = compile_task(session, "fault-ov-outage-0001")
    wait_until(lambda: db.task(job)["upstream_task_id"], 30, "submitted before outage")
    ov(cluster, "/_ctl/mode", {"mode": "unavailable"})
    wait_until(lambda: ov(cluster, "/_ctl/state")["unavailable_hits"] >= 3, 30, "worker hit outage")
    during = db.task(job)
    time.sleep(3)
    still = db.task(job)
    ov(cluster, "/_ctl/mode", {"mode": "ok"})
    ov(cluster, "/_ctl/finish", {"task_id": during["upstream_task_id"]})
    # Recovery: the attempt resumes on its own or is reclaimed after its lease.
    db.fetch(
        f"UPDATE {SCHEMA}.worker_tasks SET lease_until=now() WHERE task_id=$1 AND status<>'succeeded' "
        "RETURNING task_id",
        job,
    )
    wait_until(lambda: db.task(job)["status"] == "succeeded", 60, "succeeded after OV recovery")
    final = db.task(job)
    state = ov(cluster, "/_ctl/state")
    upstreams = [s["task_id"] for s in state["submissions"].values()]
    assert during["status"] in {"running", "reconciling"} and still["status"] != "failed", (during, still)
    assert upstreams.count(final["upstream_task_id"]) == 1
    report["openviking_outage"] = {
        "during": during,
        "after_3s": still,
        "final": final,
        "outage_503s": state["unavailable_hits"],
    }


def scenario_unknown_submit(cluster, session, db, report):
    before = len(ov(cluster, "/_ctl/state")["submissions"])
    ov(cluster, "/_ctl/mode", {"mode": "drop_submit_response"})
    job = compile_task(session, "fault-unknown-submit-0001")
    wait_until(lambda: len(ov(cluster, "/_ctl/state")["submissions"]) == before + 1, 30, "dropped submit recorded")
    wait_until(lambda: db.task(job)["status"] in {"reconciling", "retry_wait", "running"}, 30, "reconciling")
    first = db.task(job)
    db.fetch(
        f"UPDATE {SCHEMA}.worker_tasks SET lease_until=now(), available_at=now() WHERE task_id=$1 "
        "AND status<>'succeeded' RETURNING task_id",
        job,
    )
    wait_until(lambda: db.task(job)["upstream_task_id"], 60, "upstream recovered by idempotency key")
    upstream = db.task(job)["upstream_task_id"]
    ov(cluster, "/_ctl/finish", {"task_id": upstream})
    wait_until(lambda: db.task(job)["status"] == "succeeded", 60, "unknown submit reconciled")
    state = ov(cluster, "/_ctl/state")
    assert len(state["submissions"]) == before + 1, "reconcile must not resubmit"
    report["unknown_submit"] = {
        "after_drop": first,
        "final": db.task(job),
        "upstream_submissions": len(state["submissions"]) - before,
    }


def scenario_pg_outage(cluster, session, db, report):
    workers = ("worker-a", "worker-b")
    subprocess.run(["docker", "pause", cluster.args.pg_container], check=True)
    started = time.monotonic()
    try:
        # The API answers within its 30 s pool timeout; wait longer than that.
        during = [session.call("GET", "/api/knowledge-mining/config", timeout=75)[0] for _ in range(2)]
        wait_until(
            lambda: all(http("GET", cluster.url(n, "/readyz"))[0] == 503 for n in workers),
            90,
            "workers report not ready during outage",
        )
        readiness_during = {n: http("GET", cluster.url(n, "/readyz"))[0] for n in workers}
        alive_during = {n: cluster.procs[n].poll() is None for n in cluster.procs}
    finally:
        subprocess.run(["docker", "unpause", cluster.args.pg_container], check=True)
    outage = round(time.monotonic() - started, 1)
    wait_until(lambda: session.call("GET", "/api/knowledge-mining/config")[0] == 200, 60, "API recovered")
    wait_until(
        lambda: all(http("GET", cluster.url(n, "/readyz"))[0] == 200 for n in workers), 90, "workers ready after outage"
    )
    after = [session.call("GET", "/api/knowledge-mining/config")[0] for _ in range(4)]
    job = compile_task(session, "fault-pg-outage-0001")
    wait_until(lambda: db.task(job)["upstream_task_id"], 60, "worker claims after PG recovery")
    ov(cluster, "/_ctl/finish", {"task_id": db.task(job)["upstream_task_id"]})
    wait_until(lambda: db.task(job)["status"] == "succeeded", 60, "post-outage task succeeded")
    readiness = {n: http("GET", cluster.url(n, "/readyz"))[0] for n in cluster.procs if n != "ov"}
    assert all(alive_during.values()), alive_during
    assert all(code >= 500 for code in during), during
    assert all(code == 200 for code in after) and all(code == 200 for code in readiness.values())
    report["pg_outage"] = {
        "seconds": outage,
        "api_status_during": during,
        "worker_readiness_during": readiness_during,
        "processes_alive": alive_during,
        "api_status_after": after,
        "readiness_after": readiness,
        "post_outage_job": db.task(job),
    }


# --------------------------------------------------------------------------- #


def prepare_schema(args):
    async def run():
        conn = await asyncpg.connect(args.dsn)
        try:
            await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        finally:
            await conn.close()

    asyncio.run(run())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="interpreter of the installed release")
    parser.add_argument("--dsn", required=True, help="isolated PostgreSQL DSN with a non-superuser runtime role")
    parser.add_argument("--pg-container", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="te-fault-"))
    cluster = Cluster(args, workdir)
    db = DB(cluster.dsn)
    report = {}
    prepared = False
    lb = None
    try:
        prepare_schema(args)
        prepared = True
        schema = subprocess.run(
            [args.python, "-m", "teamEvolver", "schema", "apply"],
            cwd=workdir,
            env=cluster.env("schema"),
            capture_output=True,
            text=True,
            check=True,
        )
        report["schema_apply"] = schema.stdout.strip()
        report["release"] = subprocess.run(
            [
                args.python,
                "-c",
                "import teamEvolver,importlib.metadata as m;print(teamEvolver.__file__, m.version('teamEvolver'))",
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        cluster.start("ov")
        for name in ("api-a", "api-b", "worker-a", "worker-b"):
            cluster.start(name)
        lb = RoundRobin(cluster.ports["lb"], [cluster.url(n, "") for n in ("api-a", "api-b")])
        session = Session(f"http://127.0.0.1:{cluster.ports['lb']}")
        for scenario in (
            scenario_no_sticky,
            scenario_worker_crash,
            scenario_late_result,
            scenario_openviking_outage,
            scenario_unknown_submit,
            scenario_pg_outage,
        ):
            started = time.monotonic()
            scenario(cluster, session, db, report)
            report[scenario.__name__.removeprefix("scenario_")]["passed"] = True
            report[scenario.__name__.removeprefix("scenario_")]["elapsed_s"] = round(time.monotonic() - started, 1)
            print(f"[fault] {scenario.__name__}: passed", flush=True)
        report["lb_hits"] = {k.rsplit(":", 1)[1]: v for k, v in lb.hits.items()}
    finally:
        if lb:
            lb.close()
        report["shutdown_exit_codes"] = cluster.stop_all()
        if prepared:
            db.fetch(f"DROP SCHEMA {SCHEMA} CASCADE")
        for log in workdir.glob("*.log"):
            (args.evidence / f"fault-{log.name}").write_bytes(log.read_bytes())
        (args.evidence / "fault-acceptance.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({k: v.get("passed") for k, v in report.items() if isinstance(v, dict) and "passed" in v}))


if __name__ == "__main__":
    sys.exit(main())
