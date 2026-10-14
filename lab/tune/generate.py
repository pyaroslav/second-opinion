"""Generate training runs for the fine-tune: many short lab windows with randomised faults.

    python lab/tune/generate.py --target free --runs 200
    python lab/tune/generate.py --target se2 --until 2026-10-15T06:00

Everything here is new: its own tables (g_*), its own SQL and sequences. Nothing from the held-out set
(customers, counters, seq_ticket, the t_* workloads) and nothing from the dev set (w_*) is used, so the
dev runs stay a clean validation set and the held-out runs stay unseen.

Each run picks one fault (or two at once, a quarter of the time), random session counts, a random
window of 90-300 s, and sometimes a quiet background load. Runs land in reports/train/ (gitignored).
A run whose workers die is dropped; an error in one run never stops the batch.
"""

import argparse
import json
import random
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lab"))
import faults  # noqa: E402
import oracledb  # noqa: E402

OUT = ROOT / "reports" / "train"
SKUS = [f"SKU-{i:05d}" for i in range(1, 20001)]


def connect():
    c = faults.connect()
    c.call_timeout = 120_000  # ms; a stuck call must not hang the batch
    return c


def setup():
    with connect() as c:
        cur = c.cursor()
        for stmt in [
            "create table g_accounts (id number primary key, holder varchar2(60), email varchar2(80), "
            "branch number, balance number, opened date)",
            "insert /*+ append */ into g_accounts select level, 'holder ' || level, 'h' || level || '@bank.test', "
            "mod(level, 300), mod(level * 7, 10000), sysdate - mod(level, 2000) from dual connect by level <= 300000",
            "create table g_tickets (id number primary key, ref varchar2(30), account_id number, status varchar2(10), "
            "body varchar2(300))",
            "insert /*+ append */ into g_tickets select level, 'REF' || to_char(level * 13), mod(level, 300000) + 1, "
            "'OPEN', rpad('t', 250, 't') from dual connect by level <= 500000",
            "create table g_inventory (sku varchar2(20) primary key, qty number, descr varchar2(100))",
            "insert into g_inventory select 'SKU-' || to_char(level, 'FM00000'), 100, 'item ' || level "
            "from dual connect by level <= 20000",
            "create table g_audit (id number, at date, who varchar2(30), what varchar2(200))",
            "create sequence g_seq_nocache nocache",
            "create sequence g_seq_order nocache order",
            "create sequence g_seq_cached cache 1000",
        ]:
            try:
                cur.execute(stmt)
                c.commit()
            except oracledb.DatabaseError as e:
                if e.args[0].code not in (955, 1, 2260):
                    raise
        cur.execute("begin dbms_stats.gather_table_stats(user, 'G_ACCOUNTS'); "
                    "dbms_stats.gather_table_stats(user, 'G_TICKETS'); end;")


def loop(stop, dead, body, sleep=(0, 0)):
    """Run body(cur, conn) until stop. Database errors (deadlocks, timeouts) roll back and carry on;
    a lost connection marks the worker dead so the run can be dropped."""
    try:
        with connect() as c:
            cur = c.cursor()
            while not stop.is_set():
                try:
                    body(cur, c)
                except oracledb.DatabaseError as e:
                    code = getattr(e.args[0], "code", None)
                    if code in (3113, 3114, 3135, 28) or not c.is_healthy():
                        raise
                    try:
                        c.rollback()
                    except oracledb.DatabaseError:
                        raise
                if sleep[1]:
                    time.sleep(random.uniform(*sleep))
    except Exception:
        dead.append(traceback.format_exc(limit=1))


# ---- workloads: (stop, dead, params) -> None; params are drawn once per run ----

def g_healthy(stop, dead, p):
    def body(cur, c):
        i = random.randint(1, 300000)
        cur.execute("select balance from g_accounts where id = :1", [i])
        cur.fetchall()
        if random.random() < 0.3:
            cur.execute("update g_accounts set balance = balance + 1 where id = :1", [i])
            c.commit()
    loop(stop, dead, body, (0.05, p["pause"]))


def g_hard_parse(stop, dead, p):
    def body(cur, c):
        k = random.randint(1, 300000)
        if p["shape"] == 0:
            cur.execute(f"select holder, balance from g_accounts where id = {k}")
            cur.fetchall()
        elif p["shape"] == 1:
            cur.execute(f"insert into g_audit values ({k}, sysdate, 'app', 'viewed account {k}')")
            c.commit()
        else:
            cur.execute(f"select qty from g_inventory where sku = '{random.choice(SKUS)}'")
            cur.fetchall()
    loop(stop, dead, body)


def g_full_scan(stop, dead, p):
    def body(cur, c):
        if p["shape"] == 0:
            cur.execute("select id, balance from g_accounts where email = :1", [f"h{random.randint(1, 300000)}@bank.test"])
        elif p["shape"] == 1:
            cur.execute("select id, status from g_tickets where ref = :1", [f"REF{random.randint(1, 500000) * 13}"])
        else:
            cur.execute("select count(*) from g_tickets where account_id = :1 and status = 'OPEN'",
                        [random.randint(1, 300000)])
        cur.fetchall()
    loop(stop, dead, body)


def g_row_lock(stop, dead, p):
    hot = random.sample(SKUS[:50], p["hot_rows"])

    def body(cur, c):
        cur.execute("update g_inventory set qty = qty - 1 where sku = :1", [random.choice(hot)])
        time.sleep(random.uniform(0.05, p["hold"]))
        c.commit()
    loop(stop, dead, body)


def g_commit_storm(stop, dead, p):
    def body(cur, c):
        if p["shape"] == 0:
            cur.execute("insert into g_audit values (g_seq_cached.nextval, sysdate, 'svc', 'one event')")
        else:
            cur.execute("update g_accounts set balance = balance - 1 where id = :1", [random.randint(1, 300000)])
        c.commit()
    loop(stop, dead, body)


def g_seq_nocache(stop, dead, p):
    seq = "g_seq_order" if p["order"] else "g_seq_nocache"

    def body(cur, c):
        for _ in range(p["batch"]):
            if p["shape"] == 0:
                cur.execute(f"select {seq}.nextval from dual")
                cur.fetchall()
            else:
                cur.execute(f"insert into g_audit values ({seq}.nextval, sysdate, 'seq', 'x')")
        c.commit()
    loop(stop, dead, body)


def g_cpu_sql(stop, dead, p):
    def body(cur, c):
        if p["shape"] == 0:
            cur.execute("select count(*) from g_inventory a, g_inventory b "
                        "where a.qty + b.qty = :1 and a.sku < 'SKU-02500' and b.sku < 'SKU-02500'", [random.randint(150, 250)])
        else:
            cur.execute("select sum(length(regexp_replace(holder || email, '([a-z])([0-9])', '\\2\\1'))) "
                        "from g_accounts where id < :1", [random.randint(40000, 80000)])
        cur.fetchall()
    loop(stop, dead, body)


FAULTS = {  # name -> (worker, sessions range, params)
    "healthy": (g_healthy, (1, 4), lambda: {"pause": random.uniform(0.1, 0.4)}),
    "hard_parse": (g_hard_parse, (3, 8), lambda: {"shape": random.randrange(3)}),
    "full_scan": (g_full_scan, (2, 6), lambda: {"shape": random.randrange(3)}),
    "row_lock": (g_row_lock, (3, 8), lambda: {"hot_rows": random.randint(1, 3), "hold": random.uniform(0.2, 0.8)}),
    "commit_storm": (g_commit_storm, (4, 12), lambda: {"shape": random.randrange(2)}),
    "seq_nocache": (g_seq_nocache, (6, 14), lambda: {"order": random.random() < 0.5, "batch": random.choice([1, 20, 50]),
                                                     "shape": random.randrange(2)}),
    "cpu_sql": (g_cpu_sql, (1, 3), lambda: {"shape": random.randrange(2)}),
}


def plan() -> dict:
    """One run: a fault (or two), sessions, params, window, optional background."""
    names = [f for f in FAULTS if f != "healthy"]
    if random.random() < 0.15:
        causes = ["healthy"]
    elif random.random() < 0.25:
        causes = random.sample(names, 2)
    else:
        causes = [random.choice(names)]
    mix = []
    for f in causes:
        lo, hi = FAULTS[f][1]
        n = random.randint(lo, hi) if len(causes) == 1 else random.randint(lo, max(lo, (lo + hi) // 2))
        mix.append({"fault": f, "sessions": n, "params": FAULTS[f][2]()})
    background = causes != ["healthy"] and random.random() < 0.5
    if background:
        mix.append({"fault": "healthy", "sessions": random.randint(1, 2), "params": FAULTS["healthy"][2](),
                    "background": True})
    return {"causes": causes, "mix": mix, "seconds": random.choice([90, 120, 150, 180, 240, 300])}


def run(p: dict) -> Path | None:
    stop, dead = threading.Event(), []
    begin = faults.snapshot()
    threads = [threading.Thread(target=FAULTS[m["fault"]][0], args=(stop, dead, m["params"]), daemon=True)
               for m in p["mix"] for _ in range(m["sessions"])]
    started = datetime.now(timezone.utc)
    for t in threads:
        t.start()
    time.sleep(p["seconds"])
    stop.set()
    for t in threads:
        t.join(timeout=150)
    end = faults.snapshot()
    name = "+".join(p["causes"]) + f"-{faults.T['kind']}-{started:%Y%m%dT%H%M%S}"
    if dead or any(t.is_alive() for t in threads):
        print(f"DROP {name}: {len(dead)} worker(s) died, {sum(t.is_alive() for t in threads)} stuck. "
              f"{dead[0].strip().splitlines()[-1] if dead else ''}", flush=True)
        return None
    dest = OUT / name
    if faults.T["kind"] == "statspack":
        faults.statspack_report(begin, end, dest)
    else:
        dest.mkdir(parents=True, exist_ok=True)
        common = "set lines 32767 pages 0 feed off head off trimspool on long 10000000 longchunksize 10000000 verify off\n"
        (dest / "awr.html").write_text(faults.sysdba(
            common + f"select output from table(dbms_workload_repository.awr_report_html("
                     f"(select dbid from v$database), 1, {begin}, {end}));\n"))
    (dest / "truth.json").write_text(json.dumps({
        "fault": "+".join(p["causes"]), "causes": p["causes"], "mix": p["mix"], "seconds": p["seconds"],
        "set": "train", "snapshots": [begin, end], "started": started.isoformat(timespec="seconds"),
        "database": faults.T["database"], "kind": faults.T["kind"]}, indent=1))
    print(f"OK   {name} sessions={sum(m['sessions'] for m in p['mix'])} {p['seconds']}s", flush=True)
    return dest


def housekeeping():
    """Keep the insert-heavy faults from filling the tablespace (Free caps user data at ~12 GB)."""
    with connect() as c:
        cur = c.cursor()
        cur.execute("truncate table g_audit")
        cur.execute("select round(sum(bytes) / 1048576) from user_segments")
        print(f"     housekeeping: g_audit truncated, lab schema {cur.fetchone()[0]} MB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=faults.TARGETS, default="free")
    ap.add_argument("--runs", type=int, default=1000)
    ap.add_argument("--until", help="stop starting new runs after this UTC time, e.g. 2026-10-15T06:00")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--seconds", type=int, help="fixed window length (for a smoke test)")
    a = ap.parse_args()
    faults.T = faults.TARGETS[a.target]
    random.seed(a.seed)
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else None
    faults.setup()
    setup()
    done = 0
    for i in range(a.runs):
        if until and datetime.now(timezone.utc) >= until:
            break
        try:
            p = plan()
            if a.seconds:
                p["seconds"] = a.seconds
            done += run(p) is not None
        except Exception as e:  # one bad run never stops the batch
            print(f"ERR  run {i}: {type(e).__name__}: {str(e).splitlines()[0][:200]}", flush=True)
            time.sleep(30)
        if i % 10 == 9:
            try:
                housekeeping()
            except Exception as e:
                print(f"ERR  housekeeping: {e}", flush=True)
        time.sleep(10)
    print(f"done: {done} runs kept", flush=True)


if __name__ == "__main__":
    main()
