#!/usr/bin/env python3
"""Append one row to out/results.csv by COLUMN NAME, migrating the header if needed.

usage: append_result.py <results.csv> key=value [key=value ...]

The runner used to append a positional `echo a,b,c` line, which silently breaks the moment the
schema grows: old rows keep the old header while new rows carry extra fields, and every reader
(score.py's DictReader, run_matrix.sh's resume check) then disagrees about what column 15 is.
Writing by name instead means:
  - a brand-new file gets the canonical header (COLUMNS below);
  - an existing file with an older header is rewritten once, in place, with the missing columns
    added and back-filled with 0 for the rows that predate them (announced on stderr, because a
    rewrite of the checkpoint should never be silent);
  - values are quoted/escaped by the csv module, so a value containing a comma can't shift a row.

COLUMNS is the one definition of the results schema. Anything reading results.csv reads it by
name, so adding a measurement is an edit here plus the call site in run_one.sh.
"""
import csv
import os
import sys

COLUMNS = [
    "harness", "task", "domain", "difficulty", "repeat", "seed",
    "status", "pass", "wall_s",
    "toolcalls", "turns", "self_verify",
    # token accounting. out_tokens is the completion-token count (kept under its original name:
    # score.py's Efficiency has always read it); the rest were added with the cost metric.
    "prompt_tokens", "out_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "cost_usd", "tok_src", "tokps",
]

# columns added after the first release; back-filled with 0 in rows written before them
DEFAULTS = {c: "0" for c in COLUMNS}
DEFAULTS["tok_src"] = ""
DEFAULTS["status"] = ""
DEFAULTS["domain"] = ""


def migrate(path, existing_header):
    """Rewrite path with the canonical header, back-filling columns it didn't have."""
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: (r.get(c) if r.get(c) not in (None, "") else DEFAULTS[c])
                        for c in COLUMNS})
    os.replace(tmp, path)
    added = [c for c in COLUMNS if c not in existing_header]
    dropped = [c for c in existing_header if c and c not in COLUMNS]
    sys.stderr.write("note: migrated %s to the current schema (added: %s%s)\n"
                     % (path, ",".join(added) or "none",
                        "; dropped: " + ",".join(dropped) if dropped else ""))


def main():
    path = sys.argv[1]
    row = dict(DEFAULTS)
    for arg in sys.argv[2:]:
        k, _, v = arg.partition("=")
        if k in row:
            row[k] = v
        else:
            sys.stderr.write("warning: unknown results column %r (ignored)\n" % k)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != COLUMNS:
            migrate(path, header)
    else:
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(COLUMNS)

    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)


if __name__ == "__main__":
    main()
