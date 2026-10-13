"""Read a Statspack text report into the same plain dict awr.parse() returns.

Statspack is what Standard Edition shops have: no Diagnostics Pack, so no AWR and no ADDM.
The report is fixed-width text. Column names are read from the header above each dashed rule,
so small layout differences between releases don't break the parse.
"""

import re
from pathlib import Path

# Statspack label -> the AWR label rules.py reads
LOAD = {"DB time(s)": "DB Time(s)", "DB CPU(s)": "DB CPU(s)", "Redo size": "Redo size (bytes)",
        "Logical reads": "Logical read (blocks)", "Block changes": "Block changes",
        "Physical reads": "Physical read (blocks)", "Physical writes": "Physical write (blocks)",
        "User calls": "User calls", "Parses": "Parses (SQL)", "Hard parses": "Hard parses (SQL)",
        "Logons": "Logons", "Executes": "Executes (SQL)", "Rollbacks": "Rollbacks", "Transactions": "Transactions"}

SQL_SECTIONS = {"SQL ordered by CPU": "cpu", "SQL ordered by Elapsed": "elapsed", "SQL ordered by Gets": "gets",
                "SQL ordered by Reads": "reads", "SQL ordered by Executions": "execs", "SQL ordered by Parse Calls": "parses"}
SEG_SECTIONS = {"Segments by Logical Reads": "logical", "Segments by Row Lock Waits": "row_lock",
                "Segments by Buffer Busy Waits": "bbw", "Segments by Table Scans": "scans"}
# Statspack column -> AWR column, where rules.py needs it
SQL_COLS = {"Hash Value": "SQL Id", "Old Hash Value": "SQL Id", "SQL Id": "SQL Id", "Gets per Exec": "Gets per Exec",
            "Executions": "Executions", "%Total": "%Total", "CPU per Exec (s)": "CPU per Exec (s)",
            "Buffer Gets": "Buffer Gets", "CPU Time (s)": "CPU Time (s)", "Elapsd Time (s)": "Elapsed Time (s)",
            "Elapsed Time (s)": "Elapsed Time (s)", "Parse Calls": "Parse Calls", "Physical Reads": "Physical Reads"}
ACTIVITY = ["parse count (total)", "parse count (hard)", "parse count (failures)", "execute count",
            "user commits", "user rollbacks", "user calls", "redo size", "session logical reads",
            "physical reads", "table scans (long tables)", "table scans (short tables)",
            "table scan rows gotten", "sorts (disk)", "consistent gets", "db block changes",
            "enqueue waits", "session cursor cache hits", "redo synch writes"]

NUMBER = re.compile(r"^-?[\d,]*\.?\d+$")


def _num(s: str):
    s = s.strip().replace(",", "")
    if s.startswith("#"):
        return None  # sqlplus overflow marker
    try:
        return float(s)
    except ValueError:
        return s or None


def _sections(lines: list[str]) -> dict[str, list[str]]:
    """Split at section titles ('Xxx  DB/Inst: ...') and the few untitled summary blocks at the top."""
    out, name, buf = {}, "header", []
    for ln in lines:
        m = re.match(r"^(\S.*?)\s+DB/Inst:", ln)
        if m or ln.startswith("Top 5 Timed Events") or ln.startswith("Load Profile"):
            out.setdefault(name, []).extend(buf)
            name = m.group(1).strip() if m else ("top5" if ln.startswith("Top 5") else "load")
            buf = []
        buf.append(ln)
    out.setdefault(name, []).extend(buf)
    return out


def _columns(block: list[str]):
    """Find the dashed rule; column spans come from it, names from the header lines above it."""
    for i, ln in enumerate(block):
        if re.match(r"^\s*-{3,}( +-{2,})+\s*$", ln):
            spans = [m.span() for m in re.finditer(r"-+", ln)]
            hdr = [ln2 for ln2 in block[max(0, i - 3):i] if ln2.strip() and not ln2.startswith("->")]
            names = []
            for a, b in spans:
                words = " ".join(h[a:b].strip() for h in hdr if h[a:b].strip())
                names.append(re.sub(r"\s+", " ", words))
            return i, spans, names
    return None, [], []


def _sql(block: list[str]) -> list[dict]:
    i, spans, names = _columns(block)
    if i is None:
        return []
    rows, cur = [], None
    for ln in block[i + 1:]:
        if ln.strip().startswith("-----") or ln.startswith("->"):
            continue
        toks = ln.split()
        if toks and all(NUMBER.match(t) or t.startswith("#") for t in toks) and len(toks) >= len(names) - 1:
            cur = {}
            for n, t in zip(names[-len(toks):], toks):
                key = SQL_COLS.get(n, n)
                cur[key] = _num(t) if key != "SQL Id" else t
            cur["text"] = ""
            rows.append(cur)
        elif cur is not None and ln.strip().startswith("Module:"):
            continue  # client program names can be local paths; not needed for a review
        elif cur is not None and ln.strip():
            cur["text"] = (cur["text"] + ln.strip())[:300] if len(cur["text"]) < 300 else cur["text"]
        elif not ln.strip():
            cur = None
    return rows


def _segments(block: list[str]) -> list[dict]:
    i, spans, names = _columns(block)
    if i is None:
        return []
    out = []
    for ln in block[i + 1:]:
        if not ln.strip() or ln.strip().startswith("---"):
            break
        r = {n: ln[a:b if k < len(spans) - 1 else None].strip() for k, (n, (a, b)) in enumerate(zip(names, spans))}
        out.append({"Owner": r.get("Owner"), "Object Name": r.get("Object Name"), "Obj. Type": r.get("Obj. Type"),
                    **{k: _num(v) for k, v in r.items() if k not in ("Owner", "Object Name", "Obj. Type", "Tablespace",
                                                                       "Subobject Name") and isinstance(_num(v), float)}})
    return out[:3]


def parse(path: str | Path) -> dict:
    text = Path(path).read_text(errors="replace")
    lines = text.splitlines()
    sec = _sections(lines)
    out = {"source": "Statspack"}

    head = "\n".join(sec.get("header", []))
    m = re.search(r"^\s*(\S+)?\s+(\d+)\s+(\S+)\s+\d+\s+\S+\s+\S+\s+(\d+\.\d+\.\d+\.\d+\.\d+)\s+(YES|NO)", head, re.M)
    if m:
        out["db"] = {"DB Name": m.group(1) or m.group(3), "Release": m.group(4), "RAC": m.group(5), "Edition": "unknown (Statspack)"}
    m = re.search(r"Linux.*?\s(\d+)\s+(\d+)\s+\d+\s+([\d.]+)\s*$|(\S.*?)\s{2,}(\d+)\s+(\d+)\s+\d+\s+([\d.]+)\s*$", head, re.M)
    hm = re.search(r"^\s+\S+\s+(.+?)\s{2,}(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*$", head, re.M)
    if hm:
        out["host"] = {"Platform": hm.group(1).strip(), "CPUs": float(hm.group(2)), "Cores": float(hm.group(3)),
                       "Memory (GB)": float(hm.group(5))}
    b = re.search(r"Begin Snap:\s+\d+\s+(\S+ \S+)", head)
    e = re.search(r"End Snap:\s+\d+\s+(\S+ \S+)", head)
    el = re.search(r"Elapsed:\s+([\d.,]+) \(mins\)", head)
    dbt = re.search(r"DB time:\s+([\d.,]+) \(mins\)", head)
    out["window"] = {"begin": b and b.group(1), "end": e and e.group(1),
                     "elapsed_min": el and _num(el.group(1)), "db_time_min": dbt and _num(dbt.group(1))}

    lp = {}
    for ln in sec.get("load", []):
        m = re.match(r"^\s*(.+?):\s+(.*)$", ln)
        if m and m.group(1).strip() in LOAD:
            vals = m.group(2).split()
            lp[LOAD[m.group(1).strip()]] = {"per_sec": _num(vals[0]) if vals else None,
                                            "per_txn": _num(vals[1]) if len(vals) > 1 else None}
    out["load_profile"] = lp

    ev = []
    for ln in sec.get("top5", []):
        m = re.match(r"^(\S.*?)\s{2,}([\d,]+)?\s+([\d,.]+)\s+(\d+)?\s+([\d.]+)\s*$", ln)
        if m and not ln.startswith("Event"):
            ev.append({"event": m.group(1).strip(), "waits": _num(m.group(2) or ""), "time_s": _num(m.group(3)),
                       "avg_wait": (m.group(4) + "ms") if m.group(4) else "", "pct_db_time": _num(m.group(5)),
                       "wait_class": ""})
    out["top_events"] = ev  # Statspack's "%Total Call Time" is the closest it has to "% DB time"

    tm = {}
    for ln in sec.get("Time Model System Stats", []):
        m = re.match(r"^(\S.*?)\s{2,}([\d,.]+)\s+([\d.]+)?\s*$", ln)
        if m:
            tm[m.group(1).strip()] = {"time_s": _num(m.group(2)), "pct_db_time": _num(m.group(3) or "")}
    out["time_model"] = tm

    acts = {}
    for name, block in sec.items():
        if name.startswith("Instance Activity Stats"):
            for ln in block:
                m = re.match(r"^(\S.*?)\s{2,}([\d,]+)\s+([\d,.]+)\s+([\d,.]+)?\s*$", ln)
                if m:
                    acts[m.group(1).strip()] = {"total": _num(m.group(2)), "per_sec": _num(m.group(3))}
    out["activity"] = {k: acts[k] for k in ACTIVITY if k in acts}

    out["top_sql"] = {}
    for title, key in SQL_SECTIONS.items():
        block = next((v for k, v in sec.items() if k.startswith(title)), None)
        if block:
            out["top_sql"][key] = _sql(block)[:5]
    for title, key in SEG_SECTIONS.items():
        block = next((v for k, v in sec.items() if k.startswith(title)), None)
        if block:
            out.setdefault("segments", {})[key] = _segments(block)
    params = {}
    for ln in sec.get("init.ora Parameters", []):
        m = re.match(r"^([a-z_0-9]+)\s{2,}(\S.*?)\s*$", ln)
        if m:
            params[m.group(1)] = m.group(2)
    out["parameters"] = params
    return out
