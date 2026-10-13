"""Score every lab run: did each reviewer name the cause we injected?

    python lab/score.py                  # all runs under reports/lab
    python lab/score.py --model gemma4:26b
    python lab/score.py --runs reports/test  # the held-out set

Four reviewers see the same report:
  addm      Oracle's own ADDM report for the same snapshots (finding titles mapped to a cause, see ADDM_TITLES)
  raw       Gemma 4 given the raw AWR text (as much as fits), same output format as ours
  rules     our rules alone, ranked by score
  second    rules + Gemma 4 (the tool)
A reviewer passes a run with one injected cause if that cause is in its top 2; with two injected
causes, if both are in its top 3. For `healthy`, it passes if it says healthy (or ADDM reports no findings).
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from second_opinion import awr, review, rules, statspack  # noqa: E402

# ADDM names each finding from a fixed vocabulary, so map its titles, in the order it ranks them.
# "Top SQL Statements" only says which SQL used the time, not why: it counts as cpu_sql, never as full_scan.
ADDM_TITLES = [
    (r"hard parse", "hard_parse"),
    (r"row lock", "row_lock"),
    (r"commits and rollbacks", "commit_storm"),
    (r"sequence", "seq_nocache"),
    (r"resource manager cpu", "cpu_cap"),
    (r"top sql", "cpu_sql"),
]


def addm_causes(text: str) -> list[str]:
    if "no findings to report" in text.lower():
        return ["healthy"]
    found = []
    for title in re.findall(r"^\s*Finding \d+: (.+)$", text, re.M):
        cause = next((c for pat, c in ADDM_TITLES if re.search(pat, title.lower())), "other")
        found.append(cause)
    return found or ["other"]


def passed(causes: list[str], ranked: list[str]) -> bool:
    """One injected cause: it must be in the top 2. Two at once: both must be in the top 3."""
    if len(causes) == 1:
        return causes[0] in ranked[:2]
    return all(c in ranked[:3] for c in causes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", default=str(ROOT / "reports" / "lab"))
    ap.add_argument("--addm-only", action="store_true", help="re-score ADDM in an existing results file; no model calls")
    a = ap.parse_args()
    if a.addm_only:
        return rescore_addm(a)
    rows = []
    for run in sorted(Path(a.runs).glob("*/truth.json")):
        d = run.parent
        tj = json.loads(run.read_text())
        truth, causes = tj["fault"], tj.get("causes", [tj["fault"]])
        sp = (d / "statspack.txt").exists()
        parsed = statspack.parse(d / "statspack.txt") if sp else awr.parse(d / "awr.html")
        raw_text = (d / ("statspack.txt" if sp else "awr.txt")).read_text(errors="replace")
        found = rules.check(parsed)
        t0 = time.time()
        ours = review.review(parsed, found, a.model)
        t_ours = time.time() - t0
        t0 = time.time()
        raw = review.review_raw(raw_text, a.model)
        t_raw = time.time() - t0
        row = {
            "run": d.name, "truth": truth,
            "kind": "statspack" if sp else "awr",
            # Standard Edition has no ADDM: that is the customer this tool is for
            "addm": None if sp else addm_causes((d / "addm.txt").read_text(errors="replace")),
            "raw": raw["ranked_causes"], "raw_unsupported": raw["unsupported_numbers"], "raw_s": round(t_raw, 1),
            "rules": [f["cause"] for f in found],
            "second": ours["ranked_causes"], "second_unsupported": ours["unsupported_numbers"],
            "second_retried": ours["retried"], "second_s": round(t_ours, 1), "headline": ours["headline"],
        }
        for k in ("addm", "raw", "rules", "second"):
            row[k + "_pass"] = None if row[k] is None else passed(causes, row[k])
        rows.append(row)
        print(f"{truth:13} {row['kind']:9} addm={row['addm_pass']!s:5} raw={row['raw_pass']!s:5} rules={row['rules_pass']!s:5} "
              f"second={row['second_pass']!s:5} | {ours['headline'][:90]}", flush=True)
    out = ROOT / "results" / f"{Path(a.runs).name}-{(a.model or review.MODEL).replace(':', '_')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    for kind in ("awr", "statspack"):
        sub = [r for r in rows if r["kind"] == kind]
        for k in ("addm", "raw", "rules", "second"):
            got = [r[k + "_pass"] for r in sub if r[k + "_pass"] is not None]
            print(f"{kind:9} {k:7} {sum(got)}/{len(got)}" if got else f"{kind:9} {k:7} n/a")
    print("invented numbers  raw:", sum(len(r["raw_unsupported"]) for r in rows),
          " second:", sum(len(r["second_unsupported"]) for r in rows))


def rescore_addm(a):
    out = ROOT / "results" / f"{Path(a.runs).name}-{(a.model or review.MODEL).replace(':', '_')}.json"
    rows = json.loads(out.read_text())
    for row in rows:
        if row["kind"] == "awr":
            d = Path(a.runs) / row["run"]
            tj = json.loads((d / "truth.json").read_text())
            row["addm"] = addm_causes((d / "addm.txt").read_text(errors="replace"))
            row["addm_pass"] = passed(tj.get("causes", [tj["fault"]]), row["addm"])
            print(f"{row['truth']:23} addm={row['addm_pass']!s:5} {row['addm']}")
    out.write_text(json.dumps(rows, indent=1))
    got = [r["addm_pass"] for r in rows if r["addm_pass"] is not None]
    print(f"awr       addm    {sum(got)}/{len(got)}")


if __name__ == "__main__":
    main()
