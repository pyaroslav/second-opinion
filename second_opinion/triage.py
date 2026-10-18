"""Triage with the fine-tuned model: rank the causes from the parsed report alone, no rules.

The prompt here is the one the LoRA was trained and scored on (lab/tune/), so the CLI asks the model
exactly what it learned to answer. The rules and the Gemma note stay the main review; this is a
second, independent read that the CLI prints next to them.
"""

import json
import re

from . import review
from .review import CAUSES, GLOSSARY

KEEP_PARAMS = ("cursor_sharing", "cpu_count", "resource_manager_plan", "sga_target", "pga_aggregate_target",
               "db_block_size", "log_buffer", "optimizer_mode", "session_cached_cursors")
SQL_KEYS = ("SQL Id", "Executions", "Elapsed Time (s)", "CPU Time (s)", "Buffer Gets", "Gets per Exec",
            "CPU per Exec (s)", "Parse Calls", "Physical Reads", "%Total", "%CPU")


def digest(d: dict) -> dict:
    def sql(rows):
        seen, out = set(), []
        for r in rows:
            if r.get("SQL Id") in seen:
                continue  # 12.2 Statspack can list a statement twice in one section
            seen.add(r.get("SQL Id"))
            out.append({**{k: r[k] for k in SQL_KEYS if r.get(k) is not None}, "text": (r.get("text") or "")[:120]})
        return out[:3]
    return {
        "report_type": d.get("source"),
        "database": d.get("db"), "host": d.get("host"), "window": d.get("window"),
        "load_profile": {k: v.get("per_sec") for k, v in (d.get("load_profile") or {}).items()},
        "top_events": [{k: e.get(k) for k in ("event", "waits", "time_s", "avg_wait", "pct_db_time", "wait_class")}
                       for e in (d.get("top_events") or [])[:10]],
        "time_model": {k: v.get("pct_db_time") for k, v in (d.get("time_model") or {}).items()
                       if v.get("pct_db_time") is not None},
        "activity": {k: v.get("per_sec") for k, v in (d.get("activity") or {}).items()},
        "top_sql": {k: sql(v) for k, v in (d.get("top_sql") or {}).items() if v},
        "segments": {k: [{kk: vv for kk, vv in s.items() if kk not in ("Obj#", "Dataobj#")} for s in v[:2]]
                     for k, v in (d.get("segments") or {}).items() if v},
        "parameters": {k: v for k, v in (d.get("parameters") or {}).items() if k in KEEP_PARAMS},
    }


def prompt(dg: dict) -> str:
    return (
        f"You are a senior Oracle DBA. Below are the key sections of a {dg.get('report_type') or 'AWR'} report, "
        "already parsed into JSON. Decide what is wrong with this database during the report window. "
        "Rank up to three root causes, most important first, using only these labels:\n" + GLOSSARY + "\n"
        "Answer with JSON only, in exactly this form: "
        '{"ranked_causes": ["<label>", ...], "evidence": "<one sentence quoting numbers from the report>"}\n\n'
        + json.dumps(dg, separators=(",", ":"), default=str))


def answer(text: str) -> list[str] | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        got = json.loads(m.group(0)).get("ranked_causes")
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(got, list):
        return None
    return [c for c in got if c in CAUSES] or None


def triage(d: dict, model: str) -> dict:
    """Ask the tuned model (in local Ollama) for its ranking. Same context size and temperature as the evaluation."""
    dg = digest(d)
    out = review.chat(prompt(dg), model=model, ctx=16384, temperature=0.0)
    ranked = answer(out)
    ev = None
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            ev = json.loads(m.group(0)).get("evidence")
        except (json.JSONDecodeError, AttributeError):
            pass
    bad = review.unsupported(ev or "", review.allowed_numbers(dg))
    return {"model": model, "ranked_causes": ranked, "evidence": ev, "unsupported_numbers": bad}
