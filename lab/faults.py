"""Break a lab database in one known way, and keep the AWR and ADDM reports for that window.

    python lab/faults.py hard_parse            # one fault, default 240 s
    python lab/faults.py all --seconds 300     # every fault, one after another
    python lab/faults.py all --target se2      # Standard Edition 2: Statspack instead of AWR
    python lab/faults.py all --test --seconds 420   # the held-out set, scored once

Each run lands in reports/lab/<fault>-<timestamp>/ with awr.html, awr.txt, addm.txt and truth.json,
so the reviewer can be scored against a cause we know, not one we guessed.

The workload connects as an ordinary user over SQL*Net (python-oracledb, thin mode). Snapshots and
reports are taken database-wide as SYSDBA inside the container, the way a DBA would run awrrpt.sql.
"""

import argparse
import json
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import oracledb

# what the database records as the client program and machine (otherwise it records local paths)
oracledb.defaults.program = "lab-workload"
oracledb.defaults.machine = "lab-client"

# free: Oracle 26ai Free, Diagnostics Pack included -> AWR + ADDM
# se2:  Oracle 12.2 Standard Edition 2, no packs (control_management_pack_access=NONE) -> Statspack only
TARGETS = {
    "free": {"container": "w2-awr-probe", "dsn": "127.0.0.1:15299/FREEPDB1", "env": "", "pre": "", "kind": "awr",
             "database": "Oracle AI Database 26ai Free 23.26.3 (container)"},
    "se2": {"container": "w2-se2", "dsn": "127.0.0.1:15298/ORCLPDB1", "env": "export ORACLE_SID=ORCLCDB; ",
            "pre": "alter session set container=ORCLPDB1;\n", "kind": "statspack",
            "database": "Oracle Database 12.2.0.1 Standard Edition 2 (container), Statspack level 7"},
}
T = TARGETS["free"]
USER, PASSWORD = "lab", "lab_pw_2026"
OUT = Path(__file__).resolve().parents[1] / "reports" / "lab"

# what a DBA would write down as the cause; the reviewer passes if it names this in its top 2
TRUTH = {
    "healthy": "nothing wrong: light, well-behaved OLTP load",
    "hard_parse": "literal SQL without bind variables causing a hard-parse storm",
    "full_scan": "a missing index: the same query full-scans a large table on every call",
    "row_lock": "row lock contention: sessions queue on the same rows (enq: TX - row lock contention)",
    "commit_storm": "commit after every single-row insert: log file sync waits",
    "seq_nocache": "a NOCACHE ORDER sequence used by many concurrent sessions (sequence/enqueue contention)",
    "cpu_sql": "one badly written SQL statement burning CPU (a cartesian join)",
}


def sysdba(sql: str) -> str:
    r = subprocess.run(["docker", "exec", "-i", T["container"], "bash", "-c", T["env"] + "sqlplus -s / as sysdba"],
                       input=T["pre"] + sql, capture_output=True, text=True, check=True)
    return r.stdout


def snapshot() -> int:
    if T["kind"] == "statspack":
        out = sysdba("set head off feed off pages 0\nvar s number\n"
                     "begin :s := perfstat.statspack.snap(i_snap_level => 7); end;\n/\nprint s\n")
    else:
        out = sysdba("set head off feed off pages 0\n"
                     "select dbms_workload_repository.create_snapshot() from dual;\n")
    return int(out.split()[-1])


def connect():
    return oracledb.connect(user=USER, password=PASSWORD, dsn=T["dsn"])


def setup():
    """Tables the faults run against. Idempotent."""
    with connect() as c:
        cur = c.cursor()
        for stmt in [
            "create table orders (id number primary key, customer_id number, status varchar2(10), amount number, note varchar2(200))",
            "insert /*+ append */ into orders select level, mod(level, 50000), 'NEW', mod(level, 977), rpad('x', 150, 'x') from dual connect by level <= 1000000",
            "create table events (id number, created date, payload varchar2(100))",
            "create table hot (id number primary key, counter number)",
            "insert into hot select level, 0 from dual connect by level <= 5",
            "create sequence seq_nocache nocache order",
            "create sequence seq_cached cache 1000",
            # held-out set only
            "create table customers (id number primary key, email varchar2(100), region varchar2(10), created date)",
            "insert /*+ append */ into customers select level, 'user' || level || '@example.com', 'R' || mod(level, 20), sysdate - mod(level, 900) from dual connect by level <= 400000",
            "create table counters (id number primary key, n number)",
            "insert into counters select level, 0 from dual connect by level <= 1000",
            "create sequence seq_ticket nocache",
        ]:
            try:
                cur.execute(stmt)
                c.commit()
            except oracledb.DatabaseError as e:
                if e.args[0].code not in (955, 1, 2260):  # already exists
                    raise
        cur.execute("begin dbms_stats.gather_table_stats(user, 'ORDERS'); dbms_stats.gather_table_stats(user, 'CUSTOMERS'); end;")


# ---- workloads: each runs until `stop` is set ----

def w_healthy(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select amount from orders where id = :1", [random.randint(1, 1000000)])
            cur.fetchall()
            cur.execute("insert into events values (seq_cached.nextval, sysdate, 'ok')")
            c.commit()
            time.sleep(0.05)


def w_hard_parse(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute(f"select amount from orders where id = {random.randint(1, 1000000)}")
            cur.fetchall()


def w_full_scan(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select count(*) from orders where customer_id = :1", [random.randint(1, 50000)])
            cur.fetchall()


def w_row_lock(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("update hot set counter = counter + 1 where id = :1", [random.randint(1, 2)])
            time.sleep(0.2)  # the app does "something" while holding the lock
            c.commit()


def w_commit_storm(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("insert into events values (seq_cached.nextval, sysdate, 'one row, one commit')")
            c.commit()


def w_seq_nocache(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            for _ in range(50):
                cur.execute("insert into events values (seq_nocache.nextval, sysdate, 'nocache')")
            c.commit()


def w_cpu_sql(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select /*+ ordered use_nl(b) */ count(*) from orders a, orders b "
                        "where a.id < 3000 and b.id < 3000 and a.amount + b.amount = :1", [random.randint(1, 1900)])
            cur.fetchall()


# ---- held-out set: same causes, different SQL, tables, session counts; plus two runs with two causes ----
# The rules were written while looking at the runs above; these were not looked at before scoring.

def t_healthy(stop):
    with connect() as c:
        cur = c.cursor()
        n = 0
        while not stop.is_set():
            cur.execute("select email from customers where id = :1", [random.randint(1, 400000)])
            cur.fetchall()
            cur.execute("update counters set n = n + 1 where id = :1", [random.randint(1, 1000)])
            n += 1
            if n % 20 == 0:
                c.commit()
            time.sleep(0.1)


def t_hard_parse(stop):
    with connect() as c:
        cur = c.cursor()
        n = 0
        while not stop.is_set():
            k = random.randint(1, 10**9)
            cur.execute(f"insert into events values ({k}, sysdate, 'order {k} received')")
            n += 1
            if n % 100 == 0:
                c.commit()


def t_full_scan(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select id, region from customers where email = :1", [f"user{random.randint(1, 400000)}@example.com"])
            cur.fetchall()


def t_row_lock(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("update orders set status = 'PAID' where id = :1", [random.randint(1, 3)])
            time.sleep(0.5)
            c.commit()


def t_commit_storm(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("update counters set n = n + 1 where id = :1", [random.randint(1, 1000)])
            c.commit()


def t_seq_nocache(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select seq_ticket.nextval from dual")
            cur.fetchall()


def t_cpu_sql(stop):
    with connect() as c:
        cur = c.cursor()
        while not stop.is_set():
            cur.execute("select sum(length(regexp_replace(note, 'x(x)', 'y\\1z'))) from orders where id < :1",
                        [random.randint(15000, 25000)])
            cur.fetchall()


TEST = {  # name -> ([(worker, sessions), ...], [causes])
    "healthy": ([(t_healthy, 3)], ["healthy"]),
    "hard_parse": ([(t_hard_parse, 6)], ["hard_parse"]),
    "full_scan": ([(t_full_scan, 3)], ["full_scan"]),
    "row_lock": ([(t_row_lock, 4)], ["row_lock"]),
    "commit_storm": ([(t_commit_storm, 10)], ["commit_storm"]),
    "seq_nocache": ([(t_seq_nocache, 12)], ["seq_nocache"]),
    "cpu_sql": ([(t_cpu_sql, 2)], ["cpu_sql"]),
    "hard_parse+row_lock": ([(t_hard_parse, 3), (t_row_lock, 4)], ["hard_parse", "row_lock"]),
    "full_scan+commit_storm": ([(t_full_scan, 2), (t_commit_storm, 6)], ["full_scan", "commit_storm"]),
}


WORKLOADS = {  # fault -> (worker, sessions)
    "healthy": (w_healthy, 2),
    "hard_parse": (w_hard_parse, 4),
    "full_scan": (w_full_scan, 4),
    "row_lock": (w_row_lock, 6),
    "commit_storm": (w_commit_storm, 6),
    "seq_nocache": (w_seq_nocache, 8),
    "cpu_sql": (w_cpu_sql, 2),
}


def statspack_report(begin: int, end: int, dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    name = f"/tmp/sp_{begin}_{end}.txt"
    subprocess.run(["docker", "exec", "-i", T["container"], "bash", "-c",
                    T["env"] + "cd /tmp && sqlplus -s perfstat/perfstat_pw@//localhost:1521/ORCLPDB1"],
                   input=f"define begin_snap={begin}\ndefine end_snap={end}\ndefine report_name={name}\n"
                         "@/tmp/spfix/spreport\nexit\n", capture_output=True, text=True, check=True)
    r = subprocess.run(["docker", "exec", T["container"], "cat", name], capture_output=True, text=True, check=True)
    (dest / "statspack.txt").write_text(r.stdout)


def reports(begin: int, end: int, dest: Path):
    if T["kind"] == "statspack":
        return statspack_report(begin, end, dest)
    dest.mkdir(parents=True, exist_ok=True)
    common = "set lines 32767 pages 0 feed off head off trimspool on long 10000000 longchunksize 10000000 verify off\n"
    dbid = "(select dbid from v$database)"
    (dest / "awr.html").write_text(sysdba(common + f"select output from table(dbms_workload_repository.awr_report_html({dbid}, 1, {begin}, {end}));\n"))
    (dest / "awr.txt").write_text(sysdba(common + f"select output from table(dbms_workload_repository.awr_report_text({dbid}, 1, {begin}, {end}));\n"))
    task = f"so_{begin}_{end}"
    (dest / "addm.txt").write_text(sysdba(
        "set lines 200 pages 0 feed off long 10000000 longchunksize 100000\n"
        f"begin dbms_addm.delete('{task}'); exception when others then null; end;\n/\n"
        f"var t varchar2(60)\nbegin :t := '{task}'; dbms_addm.analyze_db(:t, {begin}, {end}); end;\n/\n"
        "select dbms_addm.get_report(:t) from dual;\n"))


def run(fault: str, seconds: int, test: bool = False):
    if test:
        mix, causes = TEST[fault]
    else:
        mix, causes = [WORKLOADS[fault]], [fault]
    stop = threading.Event()
    begin = snapshot()
    threads = [threading.Thread(target=w, args=(stop,), daemon=True) for w, n in mix for _ in range(n)]
    started = datetime.now(timezone.utc)
    for t in threads:
        t.start()
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=60)
    end = snapshot()
    dest = (OUT.parent / "test" if test else OUT) / f"{fault}-{T['kind']}-{started:%Y%m%dT%H%M}"
    reports(begin, end, dest)
    (dest / "truth.json").write_text(json.dumps({
        "fault": fault, "causes": causes, "cause": "; ".join(TRUTH[c] for c in causes),
        "sessions": sum(n for _, n in mix), "seconds": seconds, "set": "test" if test else "dev",
        "snapshots": [begin, end], "started": started.isoformat(timespec="seconds"),
        "database": T["database"], "kind": T["kind"]}, indent=1))
    print(fault, dest.relative_to(OUT.parents[1]), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fault", choices=sorted({*WORKLOADS, *TEST, "all"}))
    ap.add_argument("--seconds", type=int, default=240)
    ap.add_argument("--target", choices=TARGETS, default="free")
    ap.add_argument("--test", action="store_true", help="the held-out set (reports/test)")
    a = ap.parse_args()
    global T
    T = TARGETS[a.target]
    setup()
    for f in ((TEST if a.test else WORKLOADS) if a.fault == "all" else [a.fault]):
        run(f, a.seconds, a.test)
        time.sleep(20)  # let the next window start quiet


if __name__ == "__main__":
    main()
