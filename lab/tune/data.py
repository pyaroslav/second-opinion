"""The fine-tuning task: read a parsed report (no rules, no findings) and rank the causes.

The rules in second_opinion/rules.py were written by hand. The question for Tinker is whether a small
open model, tuned on lab reports, can do the same triage from the parsed numbers alone.

    digest(parsed)        -> the model's input: the parsed report, trimmed to what triage needs
    prompt(digest)        -> the instruction + JSON, identical for the base and the tuned model
    target(truth, parsed) -> the answer we train on
    answer(text)          -> ranked causes from the model's output (None if it isn't valid JSON)

digest, prompt and answer live in second_opinion/triage.py, so the CLI's --triage-model asks the
tuned model exactly what it was trained on.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from second_opinion import awr, statspack  # noqa: E402
from second_opinion.review import CAUSES, GLOSSARY  # noqa: E402,F401
from second_opinion.triage import KEEP_PARAMS, SQL_KEYS, answer, digest, prompt  # noqa: E402,F401  (the prompt the CLI uses too)


def load(run: Path) -> dict:
    return statspack.parse(run / "statspack.txt") if (run / "statspack.txt").exists() else awr.parse(run / "awr.html")


def evidence(cause: str, d: dict) -> str:
    """One sentence for the training target, built only from numbers in the parsed report."""
    lp = {k: v.get("per_sec") for k, v in (d.get("load_profile") or {}).items()}
    ev = {e["event"]: e.get("pct_db_time") for e in (d.get("top_events") or [])}
    act = {k: v.get("per_sec") for k, v in (d.get("activity") or {}).items()}

    def first(*names):
        for n in names:
            if ev.get(n) is not None:
                return n, ev[n]
        return None, None

    if cause == "hard_parse":
        return f"{lp.get('Hard parses (SQL)')} hard parses per second out of {lp.get('Parses (SQL)')} parses."
    if cause == "full_scan":
        return (f"{act.get('table scans (long tables)')} long and {act.get('table scans (short tables)')} short "
                f"table scans per second, {lp.get('Logical read (blocks)')} logical reads per second.")
    if cause == "row_lock":
        n, p = first("enq: TX - row lock contention")
        return f"{n} is {p}% of DB time." if n else "Sessions wait on row locks."
    if cause == "commit_storm":
        n, p = first("log file sync")
        return f"log file sync is {p}% of DB time at {lp.get('Transactions')} transactions per second." if n else \
            f"{lp.get('Transactions')} transactions per second."
    if cause == "seq_nocache":
        n, p = first("enq: SQ - contention", "row cache lock", "row cache mutex", "enq: SV -  contention")
        return f"{n} is {p}% of DB time." if n else "Sessions wait on sequence allocation."
    if cause == "cpu_sql":
        cpu = (d.get("top_sql") or {}).get("cpu") or (d.get("top_sql") or {}).get("elapsed") or [{}]
        top = next((s for s in cpu if any(m in (s.get("text") or "").lower() for m in MARKERS["cpu_sql"])), cpu[0])
        return f"SQL {top.get('SQL Id')} used {top.get('CPU Time (s)')} s of CPU in {top.get('Executions')} executions."
    if cause == "cpu_cap":
        n, p = first("resmgr:cpu quantum")
        return f"resmgr:cpu quantum is {p}% of DB time."
    return f"DB time is {lp.get('DB Time(s)')} seconds per second; nothing stands out."


# SQL the generator's workloads run (lab/tune/generate.py); used to check a fault reached the report
MARKERS = {"full_scan": ("g_accounts where email", "g_tickets where ref", "g_tickets where account_id"),
           "cpu_sql": ("g_inventory a, g_inventory b", "regexp_replace(holder"),
           "seq_nocache": ("g_seq_order", "g_seq_nocache")}


def sql_texts(d: dict) -> str:
    return " ".join((s.get("text") or "").lower() for v in (d.get("top_sql") or {}).values() for s in v)


def visible(cause: str, d: dict) -> bool:
    """Did the injected fault leave a trace a reader of this report could see? A fault can be injected
    and still not show: a literal-SQL flood can age another statement out of the shared pool before
    the snapshot, and then no report would show it. Those causes are dropped from the label."""
    if cause == "healthy":
        return True
    lp = {k: v.get("per_sec") or 0 for k, v in (d.get("load_profile") or {}).items()}
    ev = {e["event"]: e.get("pct_db_time") or 0 for e in (d.get("top_events") or [])}
    if cause == "hard_parse":
        return lp.get("Hard parses (SQL)", 0) >= 100 and lp.get("Hard parses (SQL)", 0) >= 0.5 * lp.get("Parses (SQL)", 1)
    if cause == "row_lock":
        return ev.get("enq: TX - row lock contention", 0) >= 1
    if cause == "commit_storm":
        return ev.get("log file sync", 0) >= 1 or lp.get("Transactions", 0) >= 200
    if cause in MARKERS:
        if any(m in sql_texts(d) for m in MARKERS[cause]):
            return True
        return cause == "seq_nocache" and any(ev.get(e, 0) >= 1 for e in ("enq: SQ - contention", "row cache lock",
                                                                          "row cache mutex", "enq: SV -  contention"))
    return True


def target(causes: list[str], d: dict) -> str:
    """The injected causes, then cpu_cap if Resource Manager visibly throttled the instance."""
    ranked = list(causes)
    quantum = next((e.get("pct_db_time") for e in (d.get("top_events") or []) if e["event"] == "resmgr:cpu quantum"), None)
    if "healthy" not in ranked and quantum and quantum >= 20 and len(ranked) < 3:
        ranked.append("cpu_cap")
    return json.dumps({"ranked_causes": ranked, "evidence": " ".join(evidence(c, d) for c in ranked[:2])})
