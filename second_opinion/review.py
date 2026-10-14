"""Gemma 4 turns the findings into a ranked diagnosis and a note a DBA can send to the client.

The model never reads numbers out of the report itself. It gets the findings computed in rules.py,
and every number in its note is checked against the numbers it was given; a note that quotes a
number it wasn't given is sent back once, and any number still unaccounted for is flagged.
"""

import json
import os
import re

import httpx

from . import tracing

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
MODEL = os.environ.get("SO_MODEL", "gemma4:e4b")

CAUSES = ["hard_parse", "full_scan", "row_lock", "commit_storm", "seq_nocache", "cpu_sql", "cpu_cap", "healthy", "other"]

SCHEMA = {"type": "object", "properties": {
    "ranked_causes": {"type": "array", "items": {"type": "string", "enum": CAUSES}, "minItems": 1, "maxItems": 3},
    "headline": {"type": "string"},
    "note": {"type": "string"}},
    "required": ["ranked_causes", "headline", "note"]}

GLOSSARY = """hard_parse = literal SQL / no bind variables; full_scan = missing index, full scans;
row_lock = row lock contention; commit_storm = committing too often (log file sync);
seq_nocache = sequence contention (NOCACHE/ORDER); cpu_sql = one CPU-heavy SQL statement;
cpu_cap = instance CPU-capped by Resource Manager; healthy = nothing significant; other = none of these."""


# One context size for every call. Ollama reloads the model whenever num_ctx changes (about 4 s here);
# Sentry showed reviews taking 5 s in the benchmark and 0.7 s from the CLI, and this was why.
CTX = int(os.environ.get("SO_NUM_CTX", 32768))


def chat(prompt: str, schema: dict | None = None, model: str | None = None, ctx: int = CTX) -> str:
    body = {"model": model or MODEL, "messages": [{"role": "user", "content": prompt}], "stream": False,
            "think": False, "options": {"temperature": 0.1, "num_ctx": ctx}}
    if schema:
        body["format"] = schema
    with tracing.span("gen_ai.chat", f"chat {body['model']}", **{"gen_ai.system": "ollama",
                      "gen_ai.request.model": body["model"], "gen_ai.request.temperature": 0.1}) as s:
        r = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=600)
        r.raise_for_status()
        j = r.json()
        s.set_data("gen_ai.usage.input_tokens", j.get("prompt_eval_count"))
        s.set_data("gen_ai.usage.output_tokens", j.get("eval_count"))
        s.set_data("gen_ai.response.model", j.get("model"))
        return j["message"]["content"].strip()


SIGNIFICANT = 0.4  # rules.py score below which a finding is context, not a cause

NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def allowed_numbers(obj) -> set[float]:
    s = set()
    if isinstance(obj, dict):
        for v in obj.values():
            s |= allowed_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            s |= allowed_numbers(v)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        s.add(float(obj))
    elif isinstance(obj, str):
        s |= set(numbers_in(obj))
    return s


def unsupported(note: str, allowed: set[float]) -> list[float]:
    """Numbers in the note that are not (a rounding of) a number we gave the model.
    Small integers (1-10: 'top 2', '3 steps') are allowed as prose."""
    bad = []
    for n in numbers_in(note):
        if 0 <= n <= 10 and n == int(n):
            continue
        if any(abs(n - a) <= max(0.051, abs(a) * 0.005) or round(a) == n for a in allowed):
            continue
        bad.append(n)
    return bad


def context(d: dict, findings: list[dict]) -> dict:
    """What the model sees: the findings plus a few lines of context, nothing else."""
    lp = d.get("load_profile") or {}
    return {
        "report_type": d.get("source"), "database": d.get("db"), "host": d.get("host"), "window": d.get("window"),
        "load": {k: lp[k]["per_sec"] for k in ("DB Time(s)", "DB CPU(s)", "Executes (SQL)", "Transactions") if k in lp},
        "top_wait_events": [{k: e[k] for k in ("event", "pct_db_time", "avg_wait")} for e in (d.get("top_events") or [])[:5]],
        # below SIGNIFICANT the rules already judged a finding not worth the client's time; the model gets
        # it labelled as such, so it can mention it without ranking it as the problem
        "findings": [f for f in findings if f["score"] >= SIGNIFICANT],
        "not_significant": [{"cause": f["cause"], "why": "too little database time to matter", **f["evidence"]}
                            for f in findings if f["score"] < SIGNIFICANT],
    }


def review(d: dict, findings: list[dict], model: str | None = None) -> dict:
    ctx = context(d, findings)
    allowed = allowed_numbers(ctx)
    prompt = (
        f"You are a senior Oracle DBA doing a first-pass review of a {d.get('source', 'AWR')} report for a client. "
        "Below are the findings that were computed from the report, with their evidence. "
        "1) Rank the most likely root causes (most important first) using these labels, taking them from "
        "`findings` only; anything under `not_significant` is not a cause (if `findings` has only healthy, "
        "rank healthy first):\n" + GLOSSARY + "\n"
        "2) Write a one-line headline. 3) Write a short note for the client (under 180 words): what is wrong, "
        "the evidence, and the first two things to do. Only quote numbers that appear in the data below, "
        "exactly as given. Do not invent SQL ids, object names or numbers. Plain text, no markdown tables.\n\n"
        + json.dumps(ctx, indent=1, default=str))
    out = json.loads(chat(prompt, SCHEMA, model))
    bad = unsupported(out["headline"] + " " + out["note"], allowed)
    retried = False
    if bad:
        retried = True
        out = json.loads(chat(prompt + "\n\nYour previous note quoted numbers that are not in the data: "
                              + ", ".join(f"{b:g}" for b in bad) + ". Rewrite it using only numbers from the data.",
                              SCHEMA, model))
        bad = unsupported(out["headline"] + " " + out["note"], allowed)
    out["unsupported_numbers"] = bad
    out["retried"] = retried
    out["numbers_checked"] = len(numbers_in(out["headline"] + " " + out["note"]))
    return out


def review_raw(report_text: str, model: str | None = None, max_chars: int = 60000) -> dict:
    """Baseline: the same model and output format, but given the raw report text instead of the findings."""
    prompt = (
        "You are a senior Oracle DBA doing a first-pass review of this AWR report for a client. "
        "Rank the most likely root causes (most important first) using these labels:\n" + GLOSSARY + "\n"
        "Then write a one-line headline and a short note (under 180 words) with the evidence and first two actions.\n\n"
        + report_text[:max_chars])
    out = json.loads(chat(prompt, SCHEMA, model))
    out["unsupported_numbers"] = unsupported(out["headline"] + " " + out["note"], set(numbers_in(report_text)))
    out["truncated"] = len(report_text) > max_chars
    return out
