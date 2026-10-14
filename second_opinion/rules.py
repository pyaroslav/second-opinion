"""The first-pass checks a DBA runs in their head, written down as code.

Each check reads the parsed report and, if it fires, returns a finding with the numbers that made it
fire. The numbers are computed here, never by the language model: the model only ranks and explains
what this file found, and every figure it is allowed to quote comes from `evidence`.
"""


MIN_AAS = 0.5  # average active sessions below which nothing is worth a client's money


def _lp(d, name, field="per_sec"):
    return ((d.get("load_profile") or {}).get(name) or {}).get(field) or 0.0


def _act(d, name, field="per_sec"):
    return ((d.get("activity") or {}).get(name) or {}).get(field) or 0.0


def _event(d, *names):
    """% DB time and avg wait of the first matching foreground event in the top list."""
    for e in d.get("top_events") or []:
        if any(e["event"].lower().startswith(n) for n in names):
            return e
    return None


def _db_cpu_pct(d):
    e = _event(d, "db cpu", "cpu time")
    return (e or {}).get("pct_db_time") or 0.0


def _top_sql(d, kind):
    return ((d.get("top_sql") or {}).get(kind) or [None])[0]


def check(d: dict) -> list[dict]:
    f = []

    def add(cause, score, summary, **evidence):
        f.append({"cause": cause, "score": round(score, 2), "summary": summary, "evidence": evidence})

    aas = _lp(d, "DB Time(s)")
    cpus = (d.get("host") or {}).get("CPUs") or 0

    # 1. hard parsing: literal SQL, no binds
    hard, parses = _lp(d, "Hard parses (SQL)"), _lp(d, "Parses (SQL)")
    if hard >= 50 and parses and hard / parses >= 0.3:
        latch = _event(d, "latch: shared pool", "library cache", "row cache", "cursor: ")
        add("hard_parse", min(1.0, hard / parses + (0.2 if latch else 0)),
            "Most parses are hard parses: SQL is being sent with literals instead of bind variables.",
            hard_parses_per_sec=hard, parses_per_sec=parses, hard_parse_pct=min(100.0, round(100 * hard / parses, 1)),
            **({"shared_pool_event": latch["event"], "shared_pool_event_pct_db_time": latch["pct_db_time"]} if latch else {}))

    # 2. full scans where an index lookup was meant: a frequent statement reading thousands of blocks
    #    per execution, AND the instance actually doing long table scans (else it's not a scan problem)
    g = _top_sql(d, "gets")
    # Oracle files a scan as "short" or "long" by a size threshold that depends on the buffer cache, so a
    # full scan of a 200 MB table can count as "short" on a big cache: count both
    scans = _act(d, "table scans (long tables)") + _act(d, "table scans (short tables)")
    seg = ((d.get("segments") or {}).get("scans") or [{}])[0]
    if g and (g.get("Gets per Exec") or 0) >= 1000 and (g.get("Executions") or 0) >= 100:
        score = 0.4 + (0.4 if scans >= 1 else 0) + (0.2 if seg.get("Object Name") else 0)
        add("full_scan", min(1.0, score),
            "One frequently run statement reads thousands of blocks per execution while the instance runs long "
            "table scans: likely a full scan where an index is missing.",
            sql_id=g.get("SQL Id"), gets_per_exec=g.get("Gets per Exec"), executions=g.get("Executions"),
            pct_of_total_gets=g.get("%Total"), table_scans_per_sec=scans,
            **({"most_scanned_table": seg.get("Object Name")} if seg.get("Object Name") else {}),
            sql_text=(g.get("text") or "")[:160])

    # 2b. full scans read from disk: multiblock reads ('db file scattered read', or 'direct path read'
    #     for serial direct reads since 11g) taking a large share of the time. In the lab the tables fit
    #     in the buffer cache, so this never fired there; on real systems it is the classic sign.
    #     (Added after reading public forum reports; see README.)
    e = _event(d, "db file scattered read", "direct path read")
    if e and (e.get("pct_db_time") or 0) >= 20 and not any(x["cause"] == "full_scan" for x in f):
        add("full_scan", min(1.0, e["pct_db_time"] / 50 + 0.2),
            "A large share of time goes to multiblock reads: full table scans read from disk, usually a missing "
            "index or a query that can't use one.",
            event=e["event"], event_pct_db_time=e["pct_db_time"], avg_wait=e.get("avg_wait"))

    # 3. row lock contention
    e = _event(d, "enq: tx - row lock contention")
    if e and (e.get("pct_db_time") or 0) >= 5:
        seg = ((d.get("segments") or {}).get("row_lock") or [{}])[0]
        add("row_lock", min(1.0, e["pct_db_time"] / 40),
            "Sessions are queuing on the same rows (enq: TX - row lock contention): an application locking pattern, not a database setting.",
            event_pct_db_time=e["pct_db_time"], waits=e.get("waits"), avg_wait=e.get("avg_wait"),
            **({"hot_segment": seg.get("Object Name")} if seg.get("Object Name") else {}))

    # 4. commit storm: log file sync
    e = _event(d, "log file sync")
    commits = _act(d, "user commits")
    txn = _lp(d, "Transactions")
    if e and (e.get("pct_db_time") or 0) >= 10:
        add("commit_storm", min(1.0, e["pct_db_time"] / 40 + (0.2 if txn >= 100 else 0)),
            "Time is going to log file sync: the application commits very often (likely after every row).",
            event_pct_db_time=e["pct_db_time"], avg_wait=e.get("avg_wait"), commits_per_sec=commits or txn,
            redo_per_txn_bytes=_lp(d, "Redo size (bytes)", "per_txn"))

    # 5. sequence contention: NOCACHE / ORDER sequences
    e = _event(d, "enq: sq - contention", "row cache lock", "row cache mutex", "enq: sv -")
    seq_sql = next((s for k in ("elapsed", "execs") for s in ((d.get("top_sql") or {}).get(k) or [])
                    if "nextval" in (s.get("text") or "").lower() or "seq$" in (s.get("text") or "").lower()), None)
    if e and (e.get("pct_db_time") or 0) >= 5 and seq_sql:
        add("seq_nocache", min(1.0, e["pct_db_time"] / 30 + 0.3),
            "Sessions wait on sequence/dictionary contention while calling NEXTVAL: the sequence is probably NOCACHE or ORDER.",
            event=e["event"], event_pct_db_time=e["pct_db_time"], sql_id=seq_sql.get("SQL Id"),
            sql_text=(seq_sql.get("text") or "")[:160])

    # 6. one CPU-heavy statement: expensive per execution (a bad plan), not thousands of cheap calls
    c = _top_sql(d, "cpu")
    if c and (c.get("%Total") or 0) >= 50 and _db_cpu_pct(d) >= 50:
        per_exec = c.get("CPU per Exec (s)") or 0
        add("cpu_sql", min(1.0, c["%Total"] / 100 * (1.0 if per_exec >= 0.1 else 0.5)),
            "One SQL statement accounts for most of the CPU: tune that statement (plan, join method) before touching the server.",
            sql_id=c.get("SQL Id"), pct_of_total_cpu=c.get("%Total"), executions=c.get("Executions"),
            cpu_per_exec_s=per_exec, db_cpu_pct_db_time=_db_cpu_pct(d), sql_text=(c.get("text") or "")[:160])

    # context, not a cause: CPU capped by Resource Manager (Free/SE2 caps, or a plan)
    e = _event(d, "resmgr:cpu quantum")
    if e and (e.get("pct_db_time") or 0) >= 10:
        add("cpu_cap", min(1.0, e["pct_db_time"] / 50),
            "Sessions wait for CPU that Resource Manager will not give them (resmgr:cpu quantum): the instance is CPU-capped.",
            event_pct_db_time=e["pct_db_time"], avg_active_sessions=aas, host_cpus=cpus)

    # a wait none of the checks above explains, taking a large share of the time: say so rather than
    # call the database healthy (e.g. single-block reads growing with the data, log switch waits)
    explained = {"db cpu", "cpu time", "resmgr:cpu quantum"} | {
        (x["evidence"].get("event") or "").lower() for x in f}
    covered = {"hard_parse": ("latch: shared pool", "library cache", "row cache", "cursor: "),
               "row_lock": ("enq: tx - row lock",), "commit_storm": ("log file sync",),
               "seq_nocache": ("enq: sq", "row cache", "enq: sv"), "full_scan": ("db file scattered", "direct path read")}
    fired = {x["cause"] for x in f}
    for e in d.get("top_events") or []:
        name = e["event"].lower()
        if name in explained or any(name.startswith(p) for c in fired for p in covered.get(c, ())):
            continue
        if (e.get("pct_db_time") or 0) >= 25:
            add("other", min(1.0, e["pct_db_time"] / 60),
                f"'{e['event']}' takes a large share of the time and none of the standard checks explains it: "
                "look at it directly.", event=e["event"], event_pct_db_time=e["pct_db_time"], avg_wait=e.get("avg_wait"))
        break  # only the biggest unexplained wait

    # a percentage of almost nothing is nothing: below MIN_AAS average active sessions the
    # database is mostly idle, and "74% of DB time is log file sync" is a few milliseconds a second
    # an excerpt or a 9i Statspack may not show DB time at all: then we can't tell busy from idle,
    # so we don't pretend to; the findings stand on their percentages alone
    known = "DB Time(s)" in (d.get("load_profile") or {})
    if known and aas < MIN_AAS:
        for x in f:
            x["score"] = round(x["score"] * 0.3, 2)
            x["evidence"]["avg_active_sessions"] = aas
    if not [x for x in f if x["cause"] != "cpu_cap" and x["score"] >= 0.4]:
        add("healthy", 0.5 if known and aas < 1 else 0.3,
            "No dominant problem: no wait event or statement stands out.",
            **({"avg_active_sessions": aas} if known else {"db_time": "not in this report"}))
    return sorted(f, key=lambda x: -x["score"])
