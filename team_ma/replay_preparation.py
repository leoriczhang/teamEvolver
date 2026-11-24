"""Recover source inputs in Workers, keeping the accepted request unchanged."""

import copy
import json

from session_ingestion.queries import SessionQueries
from team_replay.datasets.materials import collect_session_materials
from teamEvolver.managed_agents import MAClient, MAError
from teamEvolver.storage.postgres import PostgresObjectStore

from .collection import materials, runtime_snapshot
from .queries import freeze
from .repository import MARepository


async def prepare(services, task, cases, skill_name="", *, records=None):
    result = {"cases": copy.deepcopy(cases), "ma_records": copy.deepcopy(records or {}),
              "ma_errors": {}, "recoveries": []}
    for case in result["cases"]:
        provenance = case.get("provenance") or {}
        runtime = provenance.get("source_runtime") or (provenance.get("session_snapshot") or {}).get("runtime") or {}
        if runtime.get("type") != "managed-agents":
            continue
        case_id = case["case_id"]
        try:
            contract = None
            if (runtime.get("materials_missing") and provenance.get("synthesis_mode") == "cross_session"
                    and not case.get("materials") and services.extras.get("replay_input_model")):
                from team_replay.input_contract import resolve
                contract = await resolve(await services.extras["replay_input_model"](task.tenant_id), case)
            if contract and contract["mode"] == "self_contained":
                runtime = copy.deepcopy(runtime)
                runtime["materials_missing"] = []
                provenance["source_runtime"] = runtime
                provenance["input_contract"] = contract
                result["recoveries"].append({"case_id": case_id, "kind": "self_contained_case",
                                              "contract": contract})
            if runtime.get("materials_missing") or runtime.get("snapshot_missing"):
                recovered = await recover_source(services, task, case, runtime)
                provenance["source_runtime"] = recovered["runtime"]
                case["materials"] = recovered["materials"]
                provenance["input_recovery"] = recovered["evidence"]
                result["recoveries"].append({"case_id": case_id, **recovered["evidence"]})
            if case_id not in result["ma_records"]:
                result["ma_records"].update(await freeze(services, task.tenant_id, [case], skill_name))
        except FileNotFoundError:
            result["ma_errors"][case_id] = "Recorded source object unavailable"
        except (ValueError, MAError) as exc:
            # MAError messages contain only validated codes; URLs stay in source storage.
            result["ma_errors"][case_id] = str(exc)
    return result


async def recover_source(services, task, case, runtime):
    provenance = case["provenance"]
    sid = provenance.get("session_id") or (provenance.get("session_snapshot") or {}).get("session_id")
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        row = await SessionQueries(services.tx).get_session(conn, session_id=sid or "")
        connection = await MARepository(services.tx).connection(conn, runtime.get("connection_id"))
        if not row or not connection or not connection["enabled"]:
            raise ValueError("Recorded source Session unavailable or connection paused")
        raw = await PostgresObjectStore(services.tx, task.tenant_id).get(conn, row["content_ref"])
        decoded = json.loads(raw)
        source = decoded.get("session", decoded)
        if (source.get("runtime") or {}).get("connection_id") != runtime["connection_id"]:
            raise ValueError("Source Session connection differs from the recorded Case")
        secret = await services.extras["secret_repo"].resolve(
            conn, secret_ref=connection["config"]["ma_secret_ref"])
    native_id = (source.get("runtime_context") or {}).get("source_session_id")
    if not native_id:
        raise ValueError("Source native Session reference unavailable")
    factory = services.extras.get("ma_client_factory") or MAClient
    async with factory(connection["config"]["ma_base_url"], secret) as client:
        native = await client.request("GET", f"sessions/{native_id}")
        fixed_runtime = copy.deepcopy(runtime)
        if runtime.get("snapshot_missing"):
            fixed_runtime.update(await runtime_snapshot(client, native))
        if not runtime.get("materials_missing"):
            return {"runtime": fixed_runtime, "materials": copy.deepcopy(case.get("materials") or []),
                    "evidence": {"source_session_id": sid, "native_session_id": native_id,
                                 "kind": "source_snapshot_recovery"}}
        # Recover only the accepted event range, never later user inputs.
        events_ref = source.get("runtime_context", {}).get("events_ref")
        async with services.tx.tenant_tx(task.tenant_id) as conn:
            archived = ((source.get("meta") or {}).get("raw_events") or {})
            events_key = events_ref or archived.get("object_ref")
            try:
                events = json.loads(await PostgresObjectStore(services.tx, task.tenant_id).get(conn, events_key)) \
                    if events_key else None
            except FileNotFoundError:
                events = None
        if events is None:
            events = await client.history(native_id)
            boundary = (source.get("meta") or {}).get("ma_end_event_id")
            if not boundary or boundary not in {e.get("id") for e in events}:
                raise ValueError("Recorded Session event boundary unavailable")
            events = events[:next(i + 1 for i, e in enumerate(events) if e.get("id") == boundary)]
        # Native Session resources may include later user additions; only the
        # resource snapshot archived with the accepted source range is eligible.
        expected_ids = {m.get("file_id") for m in runtime.get("materials_missing") or []
                        if isinstance(m, dict) and m.get("file_id")}
        resources = (source.get("runtime_context") or {}).get("resources")
        if resources is None:
            resources = [r for r in native.get("resources") or [] if r.get("file_id") in expected_ids]
        inputs, missing = await materials(client, events, services, task.tenant_id, resources)
    if missing:
        reasons = sorted({m["reason"] for m in missing})
        raise ValueError("Input recovery incomplete: " + ", ".join(reasons))
    if not inputs or expected_ids - {m.get("original_file_id") for m in inputs}:
        raise ValueError("Input recovery incomplete: recorded attachments unavailable")
    fixed_runtime["materials_missing"] = []
    recovered = collect_session_materials([{"session_id": sid, "source_materials": inputs}])
    existing = case.get("materials") or []
    if existing:
        # Frozen custom inputs take priority; enrich modality only on identical bytes.
        metadata = {m["sha256"]: m for m in recovered}
        recovered = [{**metadata.get(m.get("sha256"), {}), **m} for m in existing]
    return {"runtime": fixed_runtime, "materials": recovered, "evidence": {
        "source_session_id": sid, "native_session_id": native_id, "kind": "source_input_recovery",
        "materials": [{k: m.get(k) for k in ("path", "sha256", "size", "input_type")} for m in recovered],
    }}
