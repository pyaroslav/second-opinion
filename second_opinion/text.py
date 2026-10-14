"""Read a text report: AWR text (awrrpt.sql, text), Statspack of any release, or a partial paste.

This is what people actually have in hand: the text report from sqlplus, or the few sections a
colleague pasted into a ticket or a forum post. Whitespace in pastes is often mangled, so rows are
read token by token (a name, then numbers), not by column position, wherever that is possible.
Sections that aren't there are simply missing from the result; rules.py works with what it gets.
"""

import re
from pathlib import Path

from .statspack import ACTIVITY, SEG_SECTIONS, SQL_COLS, _segments

LOAD = {  # every spelling seen across releases -> the AWR label rules.py reads
    "db time(s)": "DB Time(s)", "db cpu(s)": "DB CPU(s)",
    "redo size": "Redo size (bytes)", "redo size (bytes)": "Redo size (bytes)",
    "logical reads": "Logical read (blocks)", "logical read (blocks)": "Logical read (blocks)",
    "block changes": "Block changes",
    "physical reads": "Physical read (blocks)", "physical read (blocks)": "Physical read (blocks)",
    "physical writes": "Physical write (blocks)", "physical write (blocks)": "Physical write (blocks)",
    "user calls": "User calls", "parses": "Parses (SQL)", "parses (sql)": "Parses (SQL)",
    "hard parses": "Hard parses (SQL)", "hard parses (sql)": "Hard parses (SQL)",
    "logons": "Logons", "executes": "Executes (SQL)", "executes (sql)": "Executes (SQL)",
    "rollbacks": "Rollbacks", "transactions": "Transactions", "sorts": "Sorts",
}
SQL_TITLES = {"sql ordered by cpu": "cpu", "sql ordered by elapsed": "elapsed", "sql ordered by gets": "gets",
              "sql ordered by reads": "reads", "sql ordered by executions": "execs", "sql ordered by parse": "parses"}
NUM = re.compile(r"^-?[\d,]*\.?\d+([eE][+-]?\d+)?(ms|us|ns|s)?$")  # sqlplus prints big values as 6.50139E+08


def _f(tok: str):
    t = tok.replace(",", "")
    m = re.match(r"^(-?\d*\.?\d+(?:[eE][+-]?\d+)?)(ms|us|ns|s)?$", t)
    return float(m.group(1)) if m else None


def _name_and_numbers(line: str):
    """'log file sync   19,343  29.4  1.52ms  1.5 Commit' -> ('log file sync', ['19,343', ...], 'Commit')"""
    toks = line.split()
    i = next((k for k, t in enumerate(toks) if NUM.match(t) and k > 0), None)
    if i is None:
        return None, [], None
    name, rest = " ".join(toks[:i]), toks[i:]
    tail = []
    while rest and not NUM.match(rest[-1]):  # a wait class can be two words ("User I/O")
        tail.insert(0, rest.pop())
    tail = " ".join(tail) or None
    if any(not NUM.match(t) for t in rest):
        return None, [], None
    return name, rest, tail


def _sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split at section titles: 'Title    DB/Inst: ...', or a line underlined with ~~~~."""
    out, title, buf = [], "header", []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        m = re.match(r"^(\S.*?)\s+DB/Inst:", ln)
        if m or (ln.strip() and re.match(r"^\s*~{4,}", nxt) and not ln.startswith("~")):
            out.append((title, buf))
            title = (m.group(1) if m else re.split(r"\s{2,}", ln.strip())[0]).strip()
            buf = []
            continue
        buf.append(ln)
    out.append((title, buf))
    return out


def _find(secs, *prefixes):
    return [b for t, b in secs if any(t.lower().startswith(p) for p in prefixes)]


def _events(block: list[str]) -> list[dict]:
    out = []
    for ln in block:
        if not ln.strip():
            if out:
                break  # the list ends at the first blank line after it starts
            continue
        if ln.lstrip().startswith(("-", "~", "->", "Event", "Avg", "Total", "%")):
            continue
        name, nums, cls = _name_and_numbers(ln)
        if not name or not nums or name.lower().startswith(("statistic", "wait class")):
            continue
        v = [_f(t) for t in nums]
        units = [re.sub(r"[\d.,-]", "", t) for t in nums]
        e = {"event": name, "pct_db_time": v[-1], "wait_class": cls or ""}
        if len(v) == 2:          # CPU row: time, %
            e["time_s"] = v[0]
        elif len(v) >= 3:        # waits, time, [avg], %
            e.update(waits=v[0], time_s=v[1])
            if len(v) >= 4:
                e["avg_wait"] = nums[2] if units[2] else nums[2] + "ms"
        out.append(e)
    return out


def _columns(block: list[str]):
    for i, ln in enumerate(block):
        if re.match(r"^\s*-{3,}( +-{2,})+\s*$", ln):
            spans = [m.span() for m in re.finditer(r"-+", ln)]
            hdr = [h for h in block[max(0, i - 3):i] if h.strip() and not h.startswith("->")]
            names = [re.sub(r"\s+", " ", " ".join(h[a:b].strip() for h in hdr if h[a:b].strip())) for a, b in spans]
            return i, names
    return None, []


def _sql(block: list[str]) -> list[dict]:
    i, names = _columns(block)
    if i is None:
        return []
    rows, cur = [], None
    for ln in block[i + 1:]:
        s = ln.strip()
        if s.startswith(("-----", "->")):
            continue
        toks = s.split()
        is_id = lambda t: bool(re.fullmatch(r"[0-9a-z]{13}|\d{6,}", t))  # AWR sql_id or Statspack hash value
        if toks and len(toks) >= 3 and is_id(toks[-1]) and all(NUM.match(t) for t in toks[:-1]) \
                or toks and len(toks) >= len(names) - 1 and all(NUM.match(t) for t in toks):
            cur = {}
            for n, t in zip(names[-len(toks):], toks):
                key = SQL_COLS.get(n, n)
                cur[key] = t if key == "SQL Id" or is_id(t) and n == names[-1] else _f(t)
            if "SQL Id" not in cur and is_id(toks[-1]):
                cur["SQL Id"] = toks[-1]
            cur["text"] = ""
            rows.append(cur)
        elif cur is not None and s.startswith(("Module:", "PDB:", "Action:")):
            continue
        elif cur is not None and s:
            if len(cur["text"]) < 300:
                cur["text"] = (cur["text"] + " " + s).strip()[:300]
        elif not s:
            cur = None
    return rows


def _segments_text(block: list[str]) -> list[dict]:
    """AWR text prints each segment on two lines (owner + tablespace, then object + numbers) under a
    two-level header; Statspack prints one line per segment. Read the object line by its numbers."""
    rules_at = [i for i, ln in enumerate(block[:20]) if re.match(r"^\s*-{3,}( +-{2,})+\s*$", ln)]
    if not rules_at:
        return []
    i = rules_at[-1]
    _, names = _columns(block[max(0, i - 3):i + 1])  # the last rule: the object-level header
    numeric = [n for n in names if n not in ("Owner", "Object Name", "Subobject Name", "Obj. Type", "Tablespace Name")]
    out = []
    for ln in block[i + 1:]:
        toks = ln.split()
        if not toks or toks[0].startswith("-"):
            if out:
                break
            continue
        k = len(toks)
        while k > 0 and NUM.match(toks[k - 1]):
            k -= 1
        nums = toks[k:]
        if len(nums) < 2 or k == 0:
            continue
        row = {"Object Name": toks[0], "Obj. Type": toks[k - 1] if k > 1 else None}
        row.update({n: _f(t) for n, t in zip(numeric[-len(nums):], nums)})
        out.append(row)
    return out[:3]


def parse(path: str | Path) -> dict:
    text = Path(path).read_text(errors="replace").expandtabs(8)
    lines = text.splitlines()
    awr = "WORKLOAD REPOSITORY" in text.upper()
    out = {"source": "AWR (text)" if awr else "Statspack"}
    head = "\n".join(lines[:80])
    rel = re.search(r"\b(\d{1,2}\.\d+\.\d+\.\d+(?:\.\d+)?)", head)
    out["db"] = {"Release": rel.group(1)} if rel else {}
    el = re.search(r"Elapsed:\s+([\d.,]+)\s*\(mins\)", text)
    dbt = re.search(r"DB [Tt]ime:\s+([\d.,]+)\s*\(mins\)", text)
    out["window"] = {"elapsed_min": el and _f(el.group(1)), "db_time_min": dbt and _f(dbt.group(1))}

    secs = _sections(lines)
    lp = {}
    for b in _find(secs, "load profile") or [lines]:
        for ln in b:
            m = re.match(r"^\s*([A-Za-z][A-Za-z ()%/]*?)\s*:\s+(-?[\d,]*\.?\d+)\s*(-?[\d,]*\.?\d+)?", ln)
            if m and m.group(1).strip().lower() in LOAD:
                key = LOAD[m.group(1).strip().lower()]
                lp.setdefault(key, {"per_sec": _f(m.group(2)), "per_txn": _f(m.group(3)) if m.group(3) else None})
    w = out["window"]
    if "DB Time(s)" not in lp and w["elapsed_min"] and w["db_time_min"]:
        lp["DB Time(s)"] = {"per_sec": round(w["db_time_min"] / w["elapsed_min"], 2), "per_txn": None}
    out["load_profile"] = lp

    ev = []
    for b in _find(secs, "top 5 timed", "top 10 foreground", "top 5 timed foreground", "top 10 timed", "top timed"):
        ev = _events(b)
        if ev:
            break
    out["top_events"] = ev

    tm = {}
    for b in _find(secs, "time model"):
        for ln in b:
            name, nums, _ = _name_and_numbers(ln)
            if name and nums and not name.lower().startswith("statistic"):
                tm[name] = {"time_s": _f(nums[0]), "pct_db_time": _f(nums[1]) if len(nums) > 1 else None}
    out["time_model"] = tm

    acts = {}
    for b in _find(secs, "instance activity stats", "key instance activity"):
        for ln in b:
            name, nums, _ = _name_and_numbers(ln)
            if name in ACTIVITY and len(nums) >= 2:
                acts.setdefault(name, {"total": _f(nums[0]), "per_sec": _f(nums[1])})
    out["activity"] = acts

    out["top_sql"] = {}
    for title, key in SQL_TITLES.items():
        for b in _find(secs, title):
            rows = _sql(b)
            if rows:
                out["top_sql"][key] = rows[:5]
                break
    for title, key in SEG_SECTIONS.items():
        for b in _find(secs, title.lower()):
            try:
                segs = _segments_text(b) if awr else _segments(b)
            except Exception:
                segs = []
            if segs:
                out.setdefault("segments", {})[key] = segs
                break
    out["parameters"] = {}
    return out
