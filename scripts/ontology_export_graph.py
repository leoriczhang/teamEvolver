#!/usr/bin/env python3
"""导出 OV 已发布本体图：resolve 播种 + query 有界遍历（只读）。

用法示例：
  OV_ONTOLOGY_AGENT_KEY=... python3 scripts/ontology_export_graph.py \
      --url http://openviking.intsit.sfcloud.local:1080 \
      --account product_agent --user team \
      --seed-texts 专享即日 接驳 顺丰同城 货拉拉 \
      --out openviking/exports/published-graph.json

说明：
- 认证使用 OV 原生 trusted 头（X-OpenViking-Account/User），密钥从环境变量
  OV_ONTOLOGY_AGENT_KEY 读取，不落盘；
- 查询侧只能看到"证据读者授权内"的事实（来源 readers 不含当前用户时整条
  事实被过滤），导出前请确认 --user 在来源 readers 中；
- resolve 是按已发布实体 label/别名匹配的，播种词覆盖不到的实体会通过
  query 遍历沿 entity 型取值被发现（标记 discovered=true）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque

import httpx


def build_client(args):
    headers = {
        "Authorization": "Bearer " + args.key,
        "X-OpenViking-Account": args.account,
        "X-OpenViking-User": args.user,
        "X-OpenViking-Role": "user",
    }
    return httpx.Client(base_url=args.url.rstrip("/") + "/api/v1/enterprise",
                        headers=headers, timeout=args.timeout, trust_env=False)


def resolve_seed(client, texts):
    """resolve 播种：词面 → 已发布实体（label/别名匹配，权限过滤）。"""
    entities, misses = {}, []
    for text in texts:
        r = client.post("/ontology/resolve", json={"text": text})
        r.raise_for_status()
        body = r.json()
        found = 0
        for cand in body.get("candidates", []):
            eid = cand.get("entity_id")
            if eid and eid not in entities:
                entities[eid] = {**cand, "seed_text": text, "discovered": False}
                found += 1
        if not found:
            misses.append(text)
    return entities, misses


def entity_refs_in_fact(fact):
    """从事实中提取实体引用：subject 恒为实体；value 形如 ent_… 视为实体引用。"""
    refs = [fact.get("subject")]
    value = fact.get("value")
    if isinstance(value, str) and value.startswith("ent_"):
        refs.append(value)
    return [r for r in refs if r]


def traverse(client, seed_ids, *, max_hops=2, batch=50, max_assertions=200, verbose=True):
    """query 有界遍历：分批查询 → 收集事实 → 沿实体引用扩展，直到不动点。"""
    queue = deque(dict.fromkeys(seed_ids))
    seen, facts, rounds = set(), {}, 0
    while queue:
        batch_ids = []
        while queue and len(batch_ids) < batch:
            eid = queue.popleft()
            if eid not in seen:
                seen.add(eid)
                batch_ids.append(eid)
        if not batch_ids:
            break
        rounds += 1
        r = client.post("/ontology/query", json={
            "entity_ids": batch_ids,
            "max_hops": max_hops,
            "max_nodes": 100,
            "max_assertions": max_assertions,
            "max_examined_edges": 5000,
        })
        r.raise_for_status()
        for fact in r.json().get("facts", []):
            facts[fact["assertion_id"]] = fact
        for fact in r.json().get("facts", []):
            for ref in entity_refs_in_fact(fact):
                if ref not in seen:
                    queue.append(ref)
        if verbose:
            print(f"  遍历轮次 {rounds}：查询 {len(batch_ids)} 实体，累计 {len(facts)} 事实", file=sys.stderr)
    return list(facts.values()), seen, rounds


def collect_evidence(facts):
    """按 evidence_id 去重汇总所有证明。"""
    evidence = {}
    for fact in facts:
        for proof_set in fact.get("proofs", []):
            for item in proof_set:
                evidence[item["evidence_id"]] = item
    return list(evidence.values())


def main():
    parser = argparse.ArgumentParser(description="resolve + query 遍历导出 OV 已发布本体图（只读）")
    parser.add_argument("--url", required=True, help="OV 根地址，如 http://host:1080")
    parser.add_argument("--account", required=True)
    parser.add_argument("--user", required=True, help="必须是来源 readers 内的 OV 用户，否则事实被过滤")
    parser.add_argument("--key", default=os.environ.get("OV_ONTOLOGY_AGENT_KEY", ""))
    parser.add_argument("--seed-texts", nargs="*", default=[], help="resolve 播种词（实体 label/别名）")
    parser.add_argument("--seed-ids", nargs="*", default=[], help="直接给定起始 entity_id")
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", required=True, help="输出 JSON 路径")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if not args.key:
        parser.error("需要 --key 或环境变量 OV_ONTOLOGY_AGENT_KEY")
    if not args.seed_texts and not args.seed_ids:
        parser.error("需要 --seed-texts 或 --seed-ids 至少一项")

    with build_client(args) as client:
        caps = client.get("/ontology/capabilities")
        caps.raise_for_status()
        capabilities = caps.json()
        print(f"generation={capabilities['generation']} schema={capabilities['schema_revision']}", file=sys.stderr)

        versions = client.get("/assets/versions")
        versions.raise_for_status() if versions.status_code == 200 else None
        versions_body = versions.json() if versions.status_code == 200 else {"error": versions.text[:200]}

        entities, misses = resolve_seed(client, args.seed_texts)
        if misses:
            print(f"resolve 未命中播种词：{misses}", file=sys.stderr)
        print(f"resolve 播种：{len(entities)} 实体", file=sys.stderr)

        facts, visited, rounds = traverse(client, list(entities) + list(args.seed_ids), max_hops=args.max_hops)
        evidence = collect_evidence(facts)

        # 遍历发现但 resolve 未覆盖的实体（无元数据，仅记录 ID）
        for eid in visited:
            if eid not in entities:
                entities[eid] = {"entity_id": eid, "discovered": True}

    export = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "endpoint": args.url,
        "account": args.account,
        "query_user": args.user,
        "semantic_generation": capabilities.get("generation"),
        "schema_revision": capabilities.get("schema_revision"),
        "authorization_epoch": capabilities.get("authorization_epoch"),
        "published_versions": versions_body,
        "counts": {
            "entities": len(entities),
            "assertions": len(facts),
            "evidence": len(evidence),
        },
        "traversal": {
            "seed_texts": args.seed_texts,
            "seed_ids": args.seed_ids,
            "rounds": rounds,
            "visited_entities": len(visited),
            "resolve_misses": misses,
        },
        "entities": list(entities.values()),
        "assertions": facts,
        "evidence": evidence,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(export, fh, ensure_ascii=False, indent=1)
    print(f"导出完成 → {args.out}", file=sys.stderr)
    print(json.dumps(export["counts"], ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
