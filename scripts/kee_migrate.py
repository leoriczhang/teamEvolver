#!/usr/bin/env python3
"""Read-only OV export and explicitly applied KEE history import/rollback. Dry-run is the default."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg

from team_ontology.kee.contracts import canonical, digest
from teamEvolver.storage.pg_pool import validate_schema_name

TABLES = [
    "tenants",
    "principals",
    "objects",
    "source_status",
    "submissions",
    "prepared",
    "grants",
    "commits",
    "generations",
    "facts",
    "entities",
    "events",
    "projections",
    "source_cursors",
    "artifact_sessions",
]


async def run(args):
    connection = await asyncpg.connect(os.environ[args.dsn_env], command_timeout=30)
    receipt = {
        "contract": "kee.migration.v1",
        "command": args.command,
        "dry_run": not args.apply,
        "counts": {},
        "requires_enrichment": True,
        "automatic_publication": False,
    }
    try:
        if args.command == "export":
            if args.output.exists():
                raise ValueError("EXPORT_FILE_EXISTS")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            # Cursor + streaming output keeps memory independent of history size.
            temporary = args.output.with_suffix(args.output.suffix + ".partial")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    async with connection.transaction(isolation="repeatable_read", readonly=True):
                        for suffix in TABLES:
                            table = "ov_ontology_" + suffix
                            if not await connection.fetchval("SELECT to_regclass($1)", "public." + table):
                                continue
                            count = 0
                            async for row in connection.cursor(
                                f'SELECT to_jsonb(t) AS record FROM public."{table}" t WHERE tenant=$1',
                                args.account,
                                prefetch=100,
                            ):
                                value = json.loads(row["record"])
                                line = (canonical({"table": table, "record": value}) + "\n").encode()
                                output.write(line)
                                checksum.update(line)
                                count += 1
                            receipt["counts"][table] = count
                # Create-only final name, preserving an existing export during a race.
                os.link(temporary, args.output)
            finally:
                temporary.unlink(missing_ok=True)
            receipt["export_sha256"] = checksum.hexdigest()

        else:
            schema = validate_schema_name(args.schema)

            def records():
                with args.input.open() as source:
                    for line in source:
                        if len(line.encode()) > 8 * 1024 * 1024:
                            raise ValueError("MIGRATION_RECORD_SIZE_LIMIT")
                        if line.strip():
                            yield json.loads(line)

            receipt["input_sha256"] = hashlib.sha256(args.input.read_bytes()).hexdigest()
            operations = []
            async with connection.transaction():
                await connection.execute("SELECT set_config('app.tenant_id',$1,true)", args.tenant)
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", args.tenant + ":kee-migrate"
                )
                for row in records():
                    if (
                        row["table"] not in {"ov_ontology_" + s for s in TABLES}
                        or row["record"].get("tenant") != args.account
                    ):
                        raise ValueError("EXPORT_ACCOUNT_OR_TABLE_MISMATCH")
                    key = f"kee/r6/history/ov/{digest(args.account)}/{row['table']}/{digest(row['record'])}"
                    prior = await connection.fetchval(
                        f'SELECT content FROM "{schema}".objects WHERE tenant_id=$1 AND key=$2', args.tenant, key
                    )
                    payload = canonical(
                        {
                            "contract": "kee.legacy-ontology.v1",
                            "source_account": args.account,
                            "data": row,
                            "publication": "history_only_requires_enrichment",
                        }
                    ).encode()
                    if args.command == "rollback":
                        if prior and bytes(prior) != payload:
                            raise ValueError("ROLLBACK_CONTENT_CHANGED")
                        action = "delete" if prior else "absent"
                        if args.apply and prior:
                            await connection.execute(
                                f'DELETE FROM "{schema}".objects WHERE tenant_id=$1 AND key=$2', args.tenant, key
                            )
                    else:
                        if prior and bytes(prior) != payload:
                            raise ValueError("IMPORT_COLLISION")
                        action = "unchanged" if prior else "create"
                        if args.apply and not prior:
                            await connection.execute(
                                f'INSERT INTO "{schema}".objects(tenant_id,key,content) VALUES($1,$2,$3)',
                                args.tenant,
                                key,
                                payload,
                            )
                    operations.append({"key": key, "action": action, "sha256": hashlib.sha256(payload).hexdigest()})
                receipt["operations"] = operations
                receipt["counts"]["records"] = len(operations)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in receipt.items() if k != "operations"}, ensure_ascii=False))
    finally:
        await connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["export", "import", "rollback"])
    parser.add_argument("--dsn-env", required=True, help="Environment variable name; never put credentials in argv")
    parser.add_argument("--account", required=True)
    parser.add_argument("--tenant", default="")
    parser.add_argument("--schema", default="teamevolver")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "export" and not args.output:
        parser.error("export requires --output")
    if args.command != "export" and (not args.input or not args.tenant):
        parser.error("import/rollback require --input and --tenant")
    asyncio.run(run(args))
