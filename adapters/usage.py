#!/usr/bin/env python3
"""The uniform metrics line every adapter's `metrics` subcommand prints, in one place.

run_one.sh reads an adapter's output by key, so the line's shape is the adapter contract:

    toolcalls=N turns=N out_tokens=N self_verify=0|1 tools=a,b,c
    prompt_tokens=N completion_tokens=N cache_read_tokens=N cache_write_tokens=N
    reasoning_tokens=N cost_usd=X

(one line, no wrapping). `out_tokens` is the original spelling of `completion_tokens` and is
emitted as well, because score.py's Efficiency has always read it under that name.

Token semantics — the same four counts for every harness, or the totals mean nothing:
  prompt_tokens       input tokens billed at full price, EXCLUDING cache hits
  completion_tokens   generated tokens
  cache_read_tokens   input served from the provider's prompt cache
  cache_write_tokens  input written into the cache
  reasoning_tokens    thinking tokens when the harness separates them out (a subset of
                      completion_tokens for most providers — reported, never added to a total)

"prompt_tokens excludes cache hits" is a normalization, not a given: providers disagree.
Anthropic and opencode's session store report the two as disjoint, while OpenAI/OpenRouter's
`prompt_tokens` is the TOTAL input and `prompt_tokens_details.cached_tokens` is the cached part
OF it. Summing the raw fields across those two shapes double-counts every cached token — and on
an agent harness cached input is usually the largest single count, so the error is not small.
`add_usage` subtracts when it reads the cached count out of a *_details object (the OpenAI/
OpenRouter shape) and leaves it alone when the count is top-level (the disjoint shape).

A harness that doesn't instrument a count reports 0, which score.py reads as "not instrumented"
and excludes from the cost report — an invented estimate would make two harnesses' totals look
comparable when they are not.
"""
import json
import re

# Every spelling the upstream providers use for the same count. Harnesses pass their provider's
# usage object through largely untouched, so the shape follows the backend (Anthropic / OpenAI /
# OpenRouter / AI-SDK normalization) rather than the harness; reading a single spelling is how
# cache counts silently come back 0 on a provider that does report them.
USAGE_KEYS = {
    "prompt_tokens": ("input", "input_tokens", "prompt", "prompt_tokens", "inputTokens",
                      "promptTokens", "tokens_input"),
    "completion_tokens": ("output", "output_tokens", "completion", "completion_tokens",
                          "outputTokens", "completionTokens", "tokens_output"),
    "cache_read_tokens": ("cacheRead", "cache_read", "cache_read_tokens", "tokens_cache_read",
                          "cache_read_input_tokens", "cachedInputTokens", "cached_tokens",
                          "cacheReadInputTokens"),
    "cache_write_tokens": ("cacheWrite", "cache_write", "cache_write_tokens", "tokens_cache_write",
                           "cache_creation_input_tokens", "cacheCreationInputTokens"),
    "reasoning_tokens": ("reasoning", "reasoning_tokens", "reasoningTokens", "tokens_reasoning"),
}
COST_KEYS = ("cost", "cost_usd", "costUSD", "total_cost", "totalCost")

# nested objects holding the cache/reasoning counts in OpenAI/OpenRouter-shaped usage payloads.
# A count found in one of these is a BREAKDOWN of its parent count, not a separate charge.
DETAIL_KEYS = ("prompt_tokens_details", "input_tokens_details",
               "completion_tokens_details", "output_tokens_details")


def new_totals():
    return dict.fromkeys(USAGE_KEYS, 0)


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def usage_get(usage, names):
    """The first present spelling, as (value, nested) — nested=True when it came out of a
    *_details breakdown object rather than the top level."""
    for n in names:
        v = _num(usage.get(n))
        if v is not None:
            return v, False
    for dk in DETAIL_KEYS:
        d = usage.get(dk)
        if isinstance(d, dict):
            for n in names:
                v = _num(d.get(n))
                if v is not None:
                    return v, True
    return 0, False


def add_usage(totals, usage):
    """Accumulate one request's/session's usage object into totals (normalizing the cached-token
    convention, see the module docstring). Returns its reported cost in USD, or 0.0."""
    if not isinstance(usage, dict):
        return 0.0
    vals = {name: usage_get(usage, names) for name, names in USAGE_KEYS.items()}
    prompt = vals["prompt_tokens"][0]
    cache_read, cache_nested = vals["cache_read_tokens"]
    if cache_nested:
        # OpenAI/OpenRouter shape: cached_tokens is part of prompt_tokens. Bill it once.
        prompt = max(0, prompt - cache_read)
    totals["prompt_tokens"] += prompt
    totals["cache_read_tokens"] += cache_read
    for name in ("completion_tokens", "cache_write_tokens", "reasoning_tokens"):
        totals[name] += vals[name][0]
    for k in COST_KEYS:
        v = _num(usage.get(k))
        if v is not None:
            return float(v)
    return 0.0


def _objects_after(text, key):
    """Yield the JSON object that follows each `"key":` in text (brace-matched, string-aware).

    A brace counter, not a regex: a usage object can contain nested *_details objects, and a
    non-greedy regex stops at the first `}` — which is the end of the nested breakdown, so the
    cost fields after it are lost."""
    for m in re.finditer(r'"%s"\s*:\s*\{' % re.escape(key), text):
        i = m.end() - 1
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == chr(92):
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(text[i:j + 1])
                    except ValueError:
                        pass
                    break


def scan_log(path, totals=None):
    """Last-resort source: sum every `"usage": {...}` object in a harness log.

    OpenRouter (and any OpenAI-compatible endpoint) returns usage — prompt/completion tokens,
    `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens` and
    `cost` — in each response, so a harness that logs its raw responses is instrumented whether
    or not it keeps its own token ledger. Returns (totals, cost, n_found); n_found == 0 means the
    log carried no usage and the caller should keep reporting zeros rather than a guess.

    This is a fallback, not the primary source: a harness that logs *cumulative* usage per
    streamed chunk would be summed as if each chunk were a separate request. Use it only when the
    harness's own ledger (a session DB, a structured event stream) has nothing."""
    totals = new_totals() if totals is None else totals
    cost = 0.0
    found = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return totals, cost, found
    for usage in _objects_after(text, "usage"):
        if not any(usage_get(usage, names)[0] for names in USAGE_KEYS.values()):
            continue    # an all-zero/unrecognized object: adds nothing, and shouldn't count as found
        cost += add_usage(totals, usage)
        found += 1
    return totals, cost, found


def metrics_line(toolcalls, turns, self_verify, tools, totals, cost=0.0):
    """The adapter contract line. `tools` is a list of tool names in call order."""
    return ("toolcalls=%d turns=%d out_tokens=%d self_verify=%d tools=%s "
            "prompt_tokens=%d completion_tokens=%d cache_read_tokens=%d cache_write_tokens=%d "
            "reasoning_tokens=%d cost_usd=%.6f"
            % (toolcalls, turns, totals["completion_tokens"], 1 if self_verify else 0,
               ",".join(tools) if tools else "-",
               totals["prompt_tokens"], totals["completion_tokens"],
               totals["cache_read_tokens"], totals["cache_write_tokens"],
               totals["reasoning_tokens"], cost))
