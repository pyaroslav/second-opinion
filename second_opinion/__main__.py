"""Second Opinion: a first-pass review of an Oracle AWR or Statspack report, on your own machine.

    second-opinion awrrpt_1_10_11.html              # AWR (HTML, as awrrpt.sql writes it)
    second-opinion sp_10_11.lst                     # Statspack (text, as spreport.sql writes it)
    second-opinion report.html --out review.md      # also save the review
    second-opinion report.html --no-gemma           # findings only, no language model
    second-opinion report.html --triage-model second-opinion-qwen3   # add the fine-tuned model's own ranking

Nothing is uploaded. The report is parsed here, the checks run here, and Gemma 4 runs in your
local Ollama. Every number in the review is checked against the report before it is printed.
"""

import argparse
import re
import json
import sys
import time
from pathlib import Path

from . import awr, review, rules, statspack, text, tracing, triage

LABELS = {"hard_parse": "Hard parsing (literal SQL, no binds)", "full_scan": "Full scans / missing index",
          "row_lock": "Row lock contention", "commit_storm": "Committing too often (log file sync)",
          "seq_nocache": "Sequence contention (NOCACHE/ORDER)", "cpu_sql": "One CPU-heavy SQL statement",
          "cpu_cap": "CPU capped by Resource Manager", "healthy": "Nothing significant", "other": "Something else"}


def load(path: Path) -> dict:
    body = path.read_text(errors="replace")
    head = body[:4000]
    if "<html" in head.lower() or "<table" in head.lower():
        return awr.parse(path)
    if "STATSPACK report for" in head:
        d = statspack.parse(path)
        if d.get("top_events") and d.get("load_profile"):
            return d
        return text.parse(path)  # an older layout the 12.2 reader doesn't know
    if re.search(r"Load Profile|Top \d+ (Timed|Foreground)|WORKLOAD REPOSITORY", body, re.I):
        return text.parse(path)  # AWR text, or sections pasted from a report
    raise SystemExit(f"{path}: not an AWR or Statspack report (HTML or text), and no Load Profile or "
                     "Top events section found.")


def render(path: Path, d: dict, found: list[dict], r: dict | None, secs: float, t: dict | None = None) -> str:
    w, db = d.get("window") or {}, d.get("db") or {}
    out = [f"# Second Opinion: {path.name}", "",
           f"{d['source']} report · {db.get('DB Name', '?')} {db.get('Release', '')} · "
           f"{w.get('begin')} to {w.get('end')} ({w.get('elapsed_min')} min, DB time {w.get('db_time_min')} min)", ""]
    if r:
        out += [f"**{r['headline']}**", "", "Likely causes, most important first: "
                + ", ".join(LABELS.get(c, c) for c in r["ranked_causes"]), "", "## Note for the client", "", r["note"], ""]
    out += ["## Findings (computed from the report, not by the model)", ""]
    for f in found:
        ev = ", ".join(f"{k}={v}" for k, v in f["evidence"].items() if k != "sql_text")
        out.append(f"- **{LABELS.get(f['cause'], f['cause'])}** (score {f['score']}): {f['summary']}  \n  `{ev}`")
        if f["evidence"].get("sql_text"):
            out.append(f"  SQL: `{f['evidence']['sql_text'][:120]}`")
    out.append("")
    if t:
        out += [f"## Independent triage by {t['model']} (fine-tuned, no rules)", ""]
        if t["ranked_causes"]:
            out.append("Ranks: " + ", ".join(LABELS.get(c, c) for c in t["ranked_causes"]))
            if t["evidence"]:
                out.append(f"Because: {t['evidence']}")
                if t["unsupported_numbers"]:
                    out.append(f"NOT FOUND in the report: {t['unsupported_numbers']}")
            ref = [c for c in (r["ranked_causes"] if r else [f["cause"] for f in found if f["score"] >= review.SIGNIFICANT])
                   if c != "cpu_cap"]
            mine = [c for c in t["ranked_causes"] if c != "cpu_cap"]
            if ref and mine:
                if mine[0] == ref[0]:
                    out.append("Same top cause as the review above.")
                elif set(mine) == set(ref):
                    out.append("Same causes as the review above, in a different order.")
                else:
                    out.append(f"DIFFERS from the review above ({', '.join(LABELS.get(c, c) for c in ref)}): "
                               "read both before you write to the client.")
        else:
            out.append("No usable answer from the model.")
        out.append("")
    if r:
        check = ("every number in the note was found in the report" if not r["unsupported_numbers"]
                 else f"NOT FOUND in the report, check before sending: {r['unsupported_numbers']}")
        out.append(f"_Numbers checked: {r['numbers_checked']}; {check}. Model: {review.MODEL}. {secs:.1f} s, all local._")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="First-pass review of an Oracle AWR or Statspack report, locally.")
    ap.add_argument("report", type=Path)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--json", action="store_true", help="print the parsed report and findings as JSON")
    ap.add_argument("--no-gemma", action="store_true")
    ap.add_argument("--model", help=f"Ollama model (default {review.MODEL})")
    ap.add_argument("--triage-model", help="also ask a fine-tuned triage model in Ollama (e.g. second-opinion-qwen3, "
                    "see lab/tune/export.py) to rank the causes from the parsed report, with no rules")
    a = ap.parse_args()
    if a.model:
        review.MODEL = a.model

    t0 = time.time()
    with tracing.span("gen_ai.invoke_agent", "second-opinion review", **{"gen_ai.agent.name": "second-opinion"}) as top:
        with tracing.span("gen_ai.execute_tool", "parse report", **{"gen_ai.tool.name": "parse"}) as s:
            d = load(a.report)
            s.set_data("report.source", d["source"])
        with tracing.span("gen_ai.execute_tool", "rules", **{"gen_ai.tool.name": "rules"}) as s:
            found = rules.check(d)
            s.set_data("findings", [f["cause"] for f in found])
        r = None
        if not a.no_gemma:
            r = review.review(d, found)
            top.set_data("unsupported_numbers", len(r["unsupported_numbers"]))
            top.set_data("retried", r["retried"])
        t = None
        if a.triage_model:
            t = triage.triage(d, a.triage_model)
            top.set_data("triage_causes", t["ranked_causes"])
    tracing.flush()

    if a.json:
        print(json.dumps({"parsed": d, "findings": found, "review": r, "triage": t}, indent=1, default=str))
        return
    text = render(a.report, d, found, r, time.time() - t0, t)
    print(text)
    if a.out:
        a.out.write_text(text + "\n")
        print(f"\nSaved to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
