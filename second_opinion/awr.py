"""Read an AWR HTML report into a small, plain dict.

AWR HTML marks every table with a `summary` attribute ("This table displays top SQL by elapsed time"),
which has been stable across releases, so tables are found by that text, not by position.
Only the sections a DBA reads in the first ten minutes are kept; everything else stays in the report.
"""

import re
from pathlib import Path

from bs4 import BeautifulSoup

# summary text (lowercase, start of string) -> our key
TABLES = {
    "this table displays database instance information": "database",
    "this table displays host information": "host",
    "this table displays snapshot information": "snapshots",
    "this table displays load profile": "load_profile",
    "this table displays top 10 wait events by total wait time": "top_events",
    "this table displays different time model statistics": "time_model",
    "this table displays foreground wait events": "fg_events",
    "this table displays top sql by elapsed time": "sql_elapsed",
    "this table displays top sql by cpu time": "sql_cpu",
    "this table displays top sql by user i/o time": "sql_io",
    "this table displays top sql by buffer gets": "sql_gets",
    "this table displays top sql by number of executions": "sql_execs",
    "this table displays top sql by number of parse calls": "sql_parses",
    "this table displays the text of the sql statements": "sql_text",
    "this table displays instance activity statistics": "activity",
    "this table displays top segments by logical reads": "seg_logical",
    "this table displays top segments by row lock waits": "seg_row_lock",
    "this table displays top segments by table scans": "seg_scans",
    "this table displays top segments by buffer busy waits": "seg_bbw",
    "this table displays name and value of the initialization parameters": "parameters",
    "this table displays name and value of init.ora parameters": "parameters",
    "this table displays the top sessions that are blocking other sessions": "blockers",
    "this table shows the top events": "ash_events",
}

# instance activity statistics worth carrying (the rest of ~200 rows is noise for a first pass)
ACTIVITY = ["parse count (total)", "parse count (hard)", "parse count (failures)", "execute count",
            "user commits", "user rollbacks", "user calls", "redo size", "session logical reads",
            "physical reads", "table scans (long tables)", "table scans (short tables)",
            "table scan rows gotten", "sorts (disk)", "consistent gets", "db block changes",
            "enqueue waits", "session cursor cache hits", "redo synch writes"]


def _num(s: str):
    s = s.strip().replace(",", "")
    try:
        return float(s) if s else None
    except ValueError:
        return s


def _rows(table) -> list[list[str]]:
    return [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]


def _keyed(rows, key_col=0):
    """Header row + data rows -> list of dicts keyed by the (whitespace-normalised) header."""
    if not rows:
        return []
    hdr = [re.sub(r"\s+", " ", h).strip() or "name" for h in rows[0]]
    return [{h: (_num(v) if i != key_col else v) for i, (h, v) in enumerate(zip(hdr, r))} for r in rows[1:] if any(r)]


def parse(path: str | Path) -> dict:
    soup = BeautifulSoup(Path(path).read_text(errors="replace"), "lxml")
    raw = {}
    for t in soup.find_all("table"):
        s = re.sub(r"\s+", " ", (t.get("summary") or "")).strip().lower()
        for prefix, key in TABLES.items():
            if s.startswith(prefix):
                raw.setdefault(key, []).append(_rows(t))
                break

    out = {"source": "AWR"}
    db = raw.get("database", [[]])
    if db and db[0]:
        first = dict(zip(*_rows_pair(db[0])))
        out["db"] = {k: first.get(k) for k in ("DB Name", "Edition", "Release", "RAC", "CDB") if first.get(k)}
    if raw.get("host"):
        h = dict(zip(*_rows_pair(raw["host"][0])))
        out["host"] = {k: _num(h[k]) if k != "Platform" else h[k] for k in ("Platform", "CPUs", "Cores", "Memory (GB)") if h.get(k)}
    if raw.get("snapshots"):
        snaps = {r[0]: r for r in raw["snapshots"][0][1:] if r}
        b, e = snaps.get("Begin Snap:"), snaps.get("End Snap:")
        out["window"] = {"begin": b[2] if b else None, "end": e[2] if e else None,
                         "elapsed_min": _num(snaps.get("Elapsed:", [None, None, ""])[2].split()[0]) if snaps.get("Elapsed:") else None,
                         "db_time_min": _num(snaps.get("DB Time:", [None, None, ""])[2].split()[0]) if snaps.get("DB Time:") else None}
    if raw.get("load_profile"):
        out["load_profile"] = {r[0].rstrip(":"): {"per_sec": _num(r[1]), "per_txn": _num(r[2])}
                               for r in raw["load_profile"][0][1:] if len(r) >= 3 and r[0]}
    if raw.get("top_events"):
        out["top_events"] = [{"event": r[0], "waits": _num(r[1]), "time_s": _num(r[2]), "avg_wait": r[3],
                              "pct_db_time": _num(r[4]), "wait_class": r[5] if len(r) > 5 else ""}
                             for r in raw["top_events"][0][1:] if r and r[0]]
    if raw.get("time_model"):
        out["time_model"] = {r[0]: {"time_s": _num(r[1]), "pct_db_time": _num(r[2])}
                             for r in raw["time_model"][0][1:] if len(r) >= 3 and r[0]}
    if raw.get("activity"):
        acts = {r[0]: r for t in raw["activity"] for r in t[1:] if r}
        out["activity"] = {k: {"total": _num(acts[k][1]), "per_sec": _num(acts[k][2])} for k in ACTIVITY if k in acts}

    texts = {}
    for t in raw.get("sql_text", []):
        for r in t[1:]:
            if len(r) >= 2:
                texts[r[0]] = r[1][:300]
    out["top_sql"] = {}
    for key in ("sql_elapsed", "sql_cpu", "sql_io", "sql_gets", "sql_execs", "sql_parses"):
        if raw.get(key):
            rows = _keyed(raw[key][0], key_col=-1)
            for r in rows[:5]:
                sid = r.get("SQL Id")
                r.pop("SQL Text", None)
                r.pop("PDB Name", None)
                if sid in texts:
                    r["text"] = texts[sid]
            out["top_sql"][key.removeprefix("sql_")] = rows[:5]
    for key in ("seg_logical", "seg_row_lock", "seg_scans", "seg_bbw"):
        if raw.get(key):
            out.setdefault("segments", {})[key.removeprefix("seg_")] = [
                {k: v for k, v in r.items() if k in ("Owner", "Object Name", "Obj. Type") or isinstance(v, float)}
                for r in _keyed(raw[key][0])[:3]]
    if raw.get("parameters"):
        out["parameters"] = {r[0]: r[1] for t in raw["parameters"] for r in t[1:] if len(r) >= 2 and r[0]}
    if raw.get("blockers"):
        out["blockers"] = _keyed(raw["blockers"][0])[:3]
    if raw.get("ash_events"):
        out["ash_events"] = _keyed(raw["ash_events"][0])[:5]
    return out


def _rows_pair(rows):
    """A two-row header/value table -> (headers, values)."""
    return (rows[0], rows[1]) if len(rows) >= 2 else ([], [])
