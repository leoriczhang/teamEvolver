"""Prepare a TE baseline from a pinned AgentScope preset and an observed invocation."""

import argparse
import json
import os
import re
from pathlib import Path

import httpx

from team_replay.execution_inputs import public_input_messages
from team_replay.execution_plan import render_components
from team_replay.request_capture import format_message
from team_skills.execution.contracts import validate_profile

VARIABLE = re.compile(r"{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}")
# Copy only inference controls. Unknown request fields need explicit adapter support.
REQUEST_FIELDS = {
    "model",
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "stream",
    "stream_options",
    "stop",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "response_format",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "thinking",
    "reasoning_effort",
}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def checked(response):
    response.raise_for_status()
    return response.json()


def load_preset(args):
    if args.preset_file:
        payload = read(args.preset_file)
    else:
        response = httpx.get(
            args.preset_url.rstrip("/") + "/api/presets/lookup",
            params={"code": args.code, "version": args.version},
            headers={"X-Project-Id": args.project, "Authorization": "Bearer " + os.environ["PRESET_TOKEN"]},
            timeout=20,
        )
        payload = checked(response)
    if "data" in payload:
        if payload.get("code") != 200:
            raise ValueError("preset lookup returned an unsuccessful business status")
        payload = payload["data"]
    if (payload.get("code"), payload.get("version")) != (args.code, args.version):
        raise ValueError("preset code/version differ from the requested revision")
    if payload.get("projectId", args.project) != args.project:
        raise ValueError("preset project differs from the requested project")
    return payload


def render(text, values):
    part = {"component_id": "validation", "slot": "system", "content": text}
    return render_components({"components": [part], "environment": {"template_variables": values}})[0]["content"]


def prepare(preset, sample, profile_id, agent_id, user_id, runtime_type):
    request = sample["final_request"]
    if not isinstance(request.get("model"), str) or not request["model"].strip():
        raise ValueError("observed final_request.model is required")
    unknown = set(request) - REQUEST_FIELDS - {"messages"}
    if unknown:
        raise ValueError("unsupported observed request fields: " + ", ".join(sorted(unknown)))
    system, user = preset["systemPrompt"], preset.get("userPromptTemplate") or ""
    if not isinstance(system, str) or not system.strip():
        raise ValueError("systemPrompt is required")
    names = sorted(set(VARIABLE.findall(system)))
    user_names = set(VARIABLE.findall(user))
    actual = sample["variables"]
    missing = (set(names) | user_names) - set(actual)
    if missing:
        raise ValueError("missing actual variable values: " + ", ".join(sorted(missing)))
    definitions = {v["name"]: v for v in preset.get("variables", [])}
    defaults = sample.get("defaults", {})
    missing_defaults = [n for n in names if n not in defaults and definitions.get(n, {}).get("defaultValue") is None]
    if missing_defaults:
        raise ValueError("supply explicit baseline defaults for: " + ", ".join(missing_defaults))
    env = {"request": {k: v for k, v in request.items() if k != "messages"}}
    if "message_prefixes" in sample:
        env["message_prefixes"] = sample["message_prefixes"]
    if names:
        env["template_variables"] = {n: defaults[n] if n in defaults else definitions[n]["defaultValue"] for n in names}
        env["template_input_contract"] = {
            "schema_version": "teamevolver.template-input-contract.v1",
            "variable_names": names,
        }
    current = sample["current_user_message"]
    if user and render(user, {n: actual[n] for n in user_names}) != current:
        raise ValueError("rendered userPromptTemplate differs from the actual current user message")
    history = public_input_messages(sample.get("input_messages", []))
    public_input_messages([{"role": "user", "content": current}])
    expected = [{"role": "system", "content": render(system, {n: actual[n] for n in names})}]
    expected.extend(history)
    expected.append({"role": "user", "content": format_message({"environment": env}, "user", current)})
    if expected != request.get("messages"):
        raise ValueError("final request messages differ in role, order or rendered text; inspect the business adapter")
    profile = {
        "profile_id": profile_id,
        "agent_id": agent_id,
        "runtime_type": runtime_type,
        "user_id": user_id,
        "affected_skill_ids": [],
        "components": [
            {
                "component_id": profile_id + ":agent-prompt",
                "kind": "agent_prompt",
                "slot": "system",
                "order": 0,
                "content": system,
            }
        ],
        "environment": env,
    }
    validate_profile(profile)
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--preset-url")
    source.add_argument("--preset-file")
    for field in ("project", "code", "sample", "profile-id", "agent-id", "user-id", "runtime-type"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--te-url", help="Also preview using the authenticated TE API")
    parser.add_argument("--tenant")
    parser.add_argument("--register", action="store_true", help="Register the verified, previewed baseline")
    args = parser.parse_args()
    if args.register and not args.te_url or args.te_url and not args.tenant:
        parser.error("--register requires --te-url and --tenant; a preview requires --tenant")
    profile = prepare(
        load_preset(args), read(args.sample), args.profile_id, args.agent_id, args.user_id, args.runtime_type
    )
    with open(args.out, "x", opener=lambda path, flags: os.open(path, flags, 0o600), encoding="utf-8") as dest:
        json.dump(profile, dest, ensure_ascii=False, indent=2)
    print("Verified original messages; wrote registration draft:", args.out)
    if args.te_url:
        path = args.te_url.rstrip("/") + "/api/skill-evolution/execution-profiles"
        headers = {"X-Tenant-Id": args.tenant, "Authorization": "Bearer " + os.environ["TEAMEVOLVER_ROOT_API_KEY"]}
        with httpx.Client(headers=headers, timeout=20) as client:
            preview = checked(client.post(path + "/preview", json=profile))
            if preview["profile_id"] != args.profile_id or not preview.get("preview_hash"):
                raise ValueError("inconsistent registration preview")
            print("Preview:", preview["preview_hash"])
            if args.register:
                result = checked(client.post(path, json=profile))
                print("Registered:", result["profile_id"], result["current_release_id"])


if __name__ == "__main__":
    main()
