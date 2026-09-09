from __future__ import annotations

import json
import os
import sys
import time

DEFAULT_RATE_LIMITS = {
    "rateLimits": {
        "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 4102444800},
        "secondary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 4102444800},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "plus",
        "rateLimitReachedType": None,
    }
}


def _app_server() -> int:
    """Minimal JSON-RPC stdio stand-in for `codex app-server`."""
    if os.environ.get("FAKE_CODEX_APP_SERVER_HANG"):
        time.sleep(float(os.environ["FAKE_CODEX_APP_SERVER_HANG"]))
        return 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            continue
        if method == "initialize":
            print(json.dumps({"id": request_id, "result": {"userAgent": "fake-codex"}}), flush=True)
            continue
        if method == "account/rateLimits/read":
            if os.environ.get("FAKE_CODEX_APP_SERVER_ERROR"):
                error = {"code": -32000, "message": os.environ["FAKE_CODEX_APP_SERVER_ERROR"]}
                print(json.dumps({"id": request_id, "error": error}), flush=True)
                return 0
            raw = os.environ.get("FAKE_CODEX_RATE_LIMITS")
            result = json.loads(raw) if raw else DEFAULT_RATE_LIMITS
            print(json.dumps({"id": request_id, "result": result}), flush=True)
            return 0
        print(json.dumps({"id": request_id, "error": {"code": -32601, "message": "unknown method"}}), flush=True)
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if "--version" in args:
        print("codex-cli 0.150.0-test")
        return 0
    if args[:1] == ["app-server"]:
        return _app_server()
    if "exec" in args and "--help" in args:
        print("Usage: codex exec [OPTIONS] [PROMPT]")
        print("  --json")
        print("  --ignore-user-config")
        print("  --approve-for-me")
        print("  --skip-git-repo-check")
        print("  --thread-source")
        print("  -C, --cd")
        print("  resume")
        return 0
    if args[:1] != ["exec"] and "exec" not in args:
        print("unknown command", file=sys.stderr)
        return 2

    if os.environ.get("FAKE_CODEX_EARLY_EXIT"):
        print("error: unexpected argument '--bogus'", file=sys.stderr, flush=True)
        return 2

    prompt = sys.stdin.read()
    dump = os.environ.get("FAKE_CODEX_DUMP")
    if dump:
        with open(dump, "w", encoding="utf-8") as handle:
            json.dump({"argv": args, "prompt": prompt}, handle)

    if os.environ.get("FAKE_CODEX_SLEEP"):
        time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))

    if os.environ.get("FAKE_CODEX_STDERR_FAIL"):
        print("codex startup failed: invalid config", file=sys.stderr, flush=True)
        return 2

    thread_id = "thread-new"
    if "resume" in args:
        index = args.index("resume")
        if index + 1 < len(args) and args[index + 1] != "-":
            thread_id = args[index + 1]

    if os.environ.get("FAKE_CODEX_FAIL"):
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        print(
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": "quota exceeded"},
                }
            )
        )
        return 1

    if os.environ.get("FAKE_CODEX_ERROR_THEN_COMPLETE"):
        print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
        print(json.dumps({"type": "error", "message": "retryable"}))
        print(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item-1", "type": "agent_message", "text": "recovered"},
                }
            )
        )
        print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 2}}))
        return 0

    print(json.dumps({"type": "thread.started", "thread_id": thread_id}))
    print(json.dumps({"type": "turn.started"}))
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-1",
                    "type": "file_change",
                    "changes": [{"path": "src/app.py", "kind": "update"}],
                    "status": "completed",
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-2",
                    "type": "agent_message",
                    "text": "first draft",
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item-3",
                    "type": "agent_message",
                    "text": f"done:{prompt[:24]}",
                },
            }
        )
    )
    print(
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 0,
                    "output_tokens": 4,
                    "reasoning_output_tokens": 1,
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
