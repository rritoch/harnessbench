#!/usr/bin/env python3
"""Parse a pi `--mode json` JSONL run -> uniform metrics line (see adapters/usage.py).
usage: parse_pi.py <run.json>
"""
import sys, json, usage as U


EXEC = {"bash", "shell", "run", "exec", "python", "pytest", "test", "execute"}
WRITE = {"write", "edit", "create", "str_replace", "apply_patch", "patch", "multiedit"}


def main():
    path = sys.argv[1]
    turns = toolcalls = 0
    totals = U.new_totals()
    cost = 0.0
    tools = []
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        lines = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        t = o.get("type")
        if t == "turn_start":
            turns += 1
        elif t == "tool_execution_start":
            nm = o.get("name") or o.get("toolName") or (o.get("toolCall") or {}).get("name")
            toolcalls += 1
            tools.append((nm or "?").lower())
        elif t == "message_end":
            m = o.get("message", {})
            if m.get("role") == "assistant":
                cost += U.add_usage(totals, m.get("usage") or {})

    # Fallback for a backend whose usage pi doesn't surface on message_end: scan the raw log for
    # response-level usage objects (OpenRouter and any OpenAI-compatible endpoint return them).
    # Only when the structured path found nothing, so the two can't double-count.
    if not any(totals.values()):
        totals, cost, _ = U.scan_log(path, totals)

    # self_verify: an exec-ish tool after the last write-ish tool
    last_write = max([i for i, t in enumerate(tools) if t in WRITE], default=-1)
    sv = 1 if any(t in EXEC for t in tools[last_write + 1:]) and last_write >= 0 else 0
    # also count "ran something at all" if no writes but executed (e.g., data tasks)
    if last_write < 0 and any(t in EXEC for t in tools):
        sv = 1
    print(U.metrics_line(toolcalls, turns, sv, tools, totals, cost))


if __name__ == "__main__":
    main()
