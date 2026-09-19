#!/usr/bin/env bash
# Orchestrate the full benchmark matrix. Sequential (single-concurrency endpoint), resumable
# (out/results.csv is the checkpoint), randomized order, with one discarded warmup per harness.
#
# usage: run_matrix.sh [--harness pi,opencode,hermes] [--tasks all|id,id] [--repeats N]
#                      [--seed-base S] [--no-warmup] [--no-probe] [--reset]
set -u
HB="$(cd "$(dirname "$0")" && pwd)"
PY="${HB_PYTHON:-python}"

HARNESSES="pi,opencode,hermes"; TASKS="all"; REPEATS=5; SEEDBASE=1000; WARMUP=1; PROBE=""; RESET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESSES="$2"; shift 2;;
    --tasks) TASKS="$2"; shift 2;;
    --repeats) REPEATS="$2"; shift 2;;
    --seed-base) SEEDBASE="$2"; shift 2;;
    --no-warmup) WARMUP=0; shift;;
    --no-probe) PROBE="noprobe"; shift;;
    --reset) RESET=1; shift;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done

# --reset: wipe the checkpoint (results.csv) and the per-run outputs the matrix appends to, then
# EXIT. It never launches a run, so it can't assume a harness/task/repeat set you didn't ask for —
# re-run without --reset to actually start the matrix. Analysis artifacts (scores.json,
# LEADERBOARD.md, *.log, plots) are left alone. Destructive and irreversible, so it requires an
# explicit typed confirmation.
if [ "$RESET" -eq 1 ]; then
  echo "RESET will permanently delete the benchmark checkpoint and per-run outputs:"
  echo "  $HB/out/results.csv"
  echo "  $HB/out/matrix.log"
  echo "  $HB/out/server_usage.csv"
  echo "  $HB/out/flags.csv"
  echo "  $HB/runs/   (all per-run working dirs, logs, and grades)"
  echo "It will NOT touch scores.json, LEADERBOARD.md, *.log, or the plots."
  echo
  # Read the confirmation from the controlling terminal. FIRST drain any pending type-ahead: a
  # newline left in the input buffer (e.g. from the Enter that launched this command, or a paste)
  # would otherwise be read *as* the answer, which is what made an earlier run "abort" before
  # anything was typed. Then re-ask on a fumbled entry rather than aborting on the first slip; only
  # the exact word "yes" proceeds, and an empty line (or Ctrl-D) cancels.
  while IFS= read -r -t 0.1 _ </dev/tty 2>/dev/null; do :; done   # flush buffered input
  confirm=""
  while :; do
    printf 'This cannot be undone. Type "yes" to confirm (or Enter to cancel): '
    IFS= read -r confirm </dev/tty || { echo; confirm=""; }
    confirm=$(printf '%s' "$confirm" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')
    [ "$confirm" = "yes" ] && break
    if [ -z "$confirm" ]; then echo "reset aborted (no changes made)."; exit 0; fi
    echo "  got '$confirm' — type exactly 'yes', or press Enter to cancel."
  done
  rm -f "$HB/out/results.csv" "$HB/out/matrix.log" "$HB/out/server_usage.csv" "$HB/out/flags.csv"
  # runs/ can hold a working dir a lingering harness process (node/opencode/hermes) still has open,
  # which rm reports as "Device or resource busy". Capture that instead of falsely claiming success.
  rm_err=$(rm -rf "$HB/runs" 2>&1)
  if [ -n "$rm_err" ] || [ -d "$HB/runs" ]; then
    echo "reset INCOMPLETE: some paths could not be removed (a lingering harness process is"
    echo "probably still holding a working dir open):"
    [ -n "$rm_err" ] && echo "$rm_err" | sed 's/^/  /'
    echo "Kill any stray harness processes, e.g.:  taskkill //F //IM node.exe //IM opencode.exe"
    echo "then run './run_matrix.sh --reset' again."
    exit 1
  fi
  echo "reset complete. Re-run without --reset to start the matrix."
  exit 0
fi

# task list
if [ "$TASKS" = "all" ]; then
  mapfile -t TASK_IDS < <(for d in "$HB"/tasks/*/; do [ -f "$d/task.json" ] && basename "$d"; done | sort)
else
  IFS=',' read -ra TASK_IDS <<< "$TASKS"
fi
IFS=',' read -ra HLIST <<< "$HARNESSES"

RES="$HB/out/results.csv"
# match exact field positions (harness,task,domain,difficulty,repeat,seed,...) — a greedy .*
# here could false-match repeat/seed against later numeric columns and silently skip a run
done_key(){ grep -q "^$1,$2,[^,]*,[^,]*,$3,$4," "$RES" 2>/dev/null; }

# Progress goes to the console AND to out/matrix.log, so a run you walked away from (or a
# terminal that scrolled/froze) still leaves something to read. The log is append-only across
# runs; run_one.sh's own per-run output stays on the console, and each harness's full transcript
# is already in runs/<harness>/<task>/rep<N>/run.log.
LOG="$HB/out/matrix.log"
mkdir -p "$HB/out"
log(){ printf '%s %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$LOG"; }

# elapsed seconds -> compact h/m/s
dur(){ local s=$1; if [ "$s" -ge 3600 ]; then printf '%dh%02dm' $((s/3600)) $(((s%3600)/60));
       elif [ "$s" -ge 60 ]; then printf '%dm%02ds' $((s/60)) $((s%60)); else printf '%ds' "$s"; fi; }

log "HarnessBench matrix: harnesses=[$HARNESSES] tasks=${#TASK_IDS[@]} repeats=$REPEATS"
log "logging to out/matrix.log"
[ -f "$RES" ] && log "resuming; $(($(wc -l < "$RES")-1)) rows already present"

# build the run list (harness,task,repeat,seed)
RUNLIST=()
for h in "${HLIST[@]}"; do
  for t in "${TASK_IDS[@]}"; do
    for ((r=1; r<=REPEATS; r++)); do
      RUNLIST+=("$h|$t|$r|$((SEEDBASE + r))")
    done
  done
done

# deterministic-ish shuffle (interleave to spread thermal drift) without Math.random:
# sort by a hash of the line so order is stable but mixed across harness/task.
# ONE python process for the whole list, not one per entry. The per-entry version spawned a
# process per run (200 tasks x N repeats): ~20s from a plain shell, but minutes-to-wedged from an
# interactive Git Bash, where every native exe spawn allocates a Windows console — enough
# conhost churn to leave mintty itself "not responding". It also printed nothing while it ran,
# so the only symptom was a dead terminal. Same ordering as before: md5 of the line plus its
# trailing newline, ascending.
log "planning ${#RUNLIST[@]} runs..."
# NB: writes through sys.stdout.buffer. Python opens stdout in text mode on Windows and would
# translate every \n to \r\n, so each entry would reach bash with a trailing CR — and the last
# field is the SEED, which would arrive as '1001\r' and land in results.csv that way.
mapfile -t RUNLIST < <(printf '%s\n' "${RUNLIST[@]}" | "$PY" -c "
import sys, hashlib
lines = [l.rstrip('\r\n') for l in sys.stdin if l.strip()]
lines.sort(key=lambda s: hashlib.md5((s + '\n').encode()).hexdigest())
sys.stdout.buffer.write(''.join(l + '\n' for l in lines).encode())
")
[ "${#RUNLIST[@]}" -gt 0 ] || { echo "run list is empty (is '$PY' on PATH?)" >&2; exit 1; }

# warmup (discarded) per harness. Its output is discarded, so announce both ends — a silent
# 30-90s gap here is indistinguishable from a hang.
if [ "$WARMUP" -eq 1 ]; then
  for h in "${HLIST[@]}"; do
    log "== warmup $h (discarded; output suppressed, may take a minute) =="
    wstart=$(date +%s)
    bash "$HB/run_one.sh" "$h" greet_format 0 1 noprobe >/dev/null 2>&1 || true
    rm -rf "$HB/runs/$h/greet_format/rep0"
    log "== warmup $h done in $(dur $(( $(date +%s) - wstart ))) =="
  done
fi

i=0; n=${#RUNLIST[@]}; t0=$(date +%s); ran=0
for entry in "${RUNLIST[@]}"; do
  IFS='|' read -r h t r s <<< "$entry"
  i=$((i+1))
  if done_key "$h" "$t" "$r" "$s"; then
    log "[$i/$n] skip (done) $h/$t rep$r"; continue
  fi
  # no per-iteration subprocess here on purpose (the task's timeout budget is reported by
  # run_one.sh's own heartbeat, which costs nothing extra)
  log "[$i/$n] $h/$t rep$r seed$s"
  rstart=$(date +%s)
  # through tee so the run's own output (including its heartbeat) lands in the log as well —
  # one tee per run, not per loop iteration. run_one.sh's exit code is not consulted here.
  bash "$HB/run_one.sh" "$h" "$t" "$r" "$s" "$PROBE" 2>&1 | tee -a "$LOG"
  now=$(date +%s); ran=$((ran+1)); elapsed=$((now - t0))
  # ETA from this session's own average, over the runs still left (skips cost nothing)
  eta=$(( (elapsed / ran) * (n - i) ))
  log "[$i/$n] done in $(dur $((now - rstart))) | elapsed $(dur $elapsed) | ETA $(dur $eta)"
done

log "matrix complete in $(dur $(( $(date +%s) - t0 ))). score with:  python score.py"
