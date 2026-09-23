#!/usr/bin/env python3
"""Read-only Agent example: direct OV access, preserving its complete ContextPacket."""

import argparse
import json
import os
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="只读 Ontology Agent；不会执行补证据工具或业务动作")
    parser.add_argument("--entity", default="Shipment:001")
    parser.add_argument("--claim", default="客户陈述尚未收到；此陈述不作为系统事实")
    parser.add_argument("--task", default="readonly-example")
    parser.add_argument("--url", default="http://127.0.0.1:52211")
    parser.add_argument("--lab", action="store_true", help="仅使用隔离环境生成的只读 agent 凭证")
    parser.add_argument("--observations-file", type=Path, help="本主体、本任务的受信观察句柄 JSON 数组")
    args = parser.parse_args()
    key = os.environ.get("OV_ONTOLOGY_AGENT_KEY", "")
    if args.lab:
        root = Path(os.environ.get("ONTOLOGY_LAB_STATE", "/tmp/te-ontology-v5"))
        key = json.loads((root / "credentials.json").read_text())["agent"]
        if args.url != "http://127.0.0.1:52211":
            parser.error("隔离凭证仅允许发送到固定本机 OV 地址")
    if not key:
        parser.error("设置 OV_ONTOLOGY_AGENT_KEY，或使用 --lab")
    observations = json.loads(args.observations_file.read_text()) if args.observations_file else []
    with httpx.Client(
        base_url=args.url.rstrip("/") + "/api/v1/enterprise",
        headers={"Authorization": "Bearer " + key},
        timeout=10,
        trust_env=False,
    ) as client:
        capabilities = client.get("/ontology/capabilities")
        capabilities.raise_for_status()
        if capabilities.json()["contract"] != "sf.ontology.v1":
            raise RuntimeError("CONTRACT_VERSION_MISMATCH")
        response = client.post(
            "/context/compose",
            json={
                "entity_ids": [args.entity],
                "task_ref": args.task,
                "claim": args.claim,
                "token_budget": 4000,
                "observation_handles": observations,
            },
        )
        response.raise_for_status()
        packet = response.json()
        # The server's renderer uses the same facts/proofs as the structured packet.
        # Do not promote missing/expired evidence to FALSE or customer claims to system facts.
        print(packet["prompt_fragment"])
        print(
            json.dumps(
                {
                    "generation": packet["semantic_generation"],
                    "degraded": packet["degraded"],
                    "truncated": packet["truncated"],
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
