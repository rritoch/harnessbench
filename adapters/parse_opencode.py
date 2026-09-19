#!/usr/bin/env python3
"""Parse an opencode `run` log + its SQLite session DB -> uniform metrics line (adapters/usage.py).
usage: parse_opencode.py <run.log> <workdir>
"""
import sys, os, re, sqlite3, usage as U

# opencode's session row already totals the whole session's usage, under its own column names —
# and reports input and cached input as DISJOINT counts (usage.add_usage leaves top-level cache
# counts alone; see its docstring).
SESSION_COLS = ("tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
                "tokens_reasoning", "cost")


def db_path():
    for p in [os.path.expanduser("~/.local/share/opencode/opencode.db"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "opencode", "opencode.db")]:
        if os.path.exists(p):
            return p
    return None


def norm(p):
    """Absolute path in the spelling opencode stores: forward slashes, lowercased."""
    return os.path.abspath(p).replace(os.sep, "/").rstrip("/").lower()


def session_ids(cur, workdir):
    """The session opencode ran in this workdir, plus every child session it spawned.

    Sub-agent sessions are separate rows with their own usage totals and are billed to the same
    task, so a task's cost is the whole tree, not just the root. Matching is on the exact absolute
    directory: a substring match can attach the wrong run, since `rep1/work` is every task's tail."""
    wd = norm(workdir)
    cur.execute("SELECT id, directory FROM session ORDER BY time_created DESC LIMIT 500")
    root = None
    for r in cur.fetchall():
        if norm(r["directory"] or "") == wd:
            root = r["id"]
            break
    if root is None:
        return []
    ids, frontier = [root], [root]
    while frontier:
        cur.execute("SELECT id FROM session WHERE parent_id IN (%s)" % ",".join("?" * len(frontier)),
                    frontier)
        frontier = [r["id"] for r in cur.fetchall() if r["id"] not in ids]
        ids.extend(frontier)
    return ids


def main():
    runlog, workdir = sys.argv[1], sys.argv[2]
    tools = []
    try:
        raw = open(runlog, encoding="utf-8", errors="replace").read()
    except Exception:
        raw = ""
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("→"):       # -> Read
            tools.append("read")
        elif s.startswith("←"):     # <- Write/Edit
            tools.append("write")
        elif s.startswith("$"):          # $ shell command
            tools.append("bash")
    toolcalls = len(tools)
    last_write = max([i for i, t in enumerate(tools) if t == "write"], default=-1)
    sv = 1 if (last_write >= 0 and "bash" in tools[last_write + 1:]) else (
        1 if last_write < 0 and "bash" in tools else 0)

    turns = 0
    cost = 0.0
    totals = U.new_totals()
    dbp = db_path()
    if dbp:
        try:
            con = sqlite3.connect(dbp); con.row_factory = sqlite3.Row; cur = con.cursor()
            for sid in session_ids(cur, workdir):
                cur.execute("SELECT %s FROM session WHERE id=?" % ",".join(SESSION_COLS), (sid,))
                row = cur.fetchone()
                if row is None:
                    continue
                cost += U.add_usage(totals, {c: row[c] for c in SESSION_COLS})
                cur.execute("SELECT COUNT(*) c FROM message WHERE session_id=?", (sid,))
                turns += cur.fetchone()["c"] or 0
        except Exception:
            pass

    # Fallback when the session row can't be found (DB moved, session pruned): the run log itself,
    # if the harness logged the provider's response usage. Only when the DB found nothing, so the
    # two sources can't double-count.
    if not any(totals.values()):
        totals, cost, _ = U.scan_log(runlog, totals)

    print(U.metrics_line(toolcalls, turns, sv, tools, totals, cost))


if __name__ == "__main__":
    main()
