"""Isolated generic JSON Compile worker. No arbitrary tools, filesystem writes or OV credentials.

Parent owns queue/lease/model budget; this subprocess only makes one bounded model call.
Input/output are stdin/stdout, never a credential-bearing command line or persistent queue.
"""

import json
import sys


def main():
    data = json.load(sys.stdin)
    from openai import OpenAI

    client = OpenAI(api_key=data["model"]["api_key"], base_url=data["model"]["base_url"], timeout=60, max_retries=0)
    result = client.chat.completions.create(
        model=data["model"]["model"],
        messages=data["messages"],
        temperature=0,
        max_completion_tokens=min(16000, max(1, int(data["model"].get("max_output_tokens", 16000)))),
        response_format={"type": "json_object"},
    )
    usage = result.usage
    print(
        json.dumps(
            {
                "content": result.choices[0].message.content,
                "usage": {"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens},
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Provider exceptions can contain request data; return only a safe class name.
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
