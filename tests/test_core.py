from pathlib import Path

import pytest

from second_opinion import awr, review, rules, statspack, text

LAB = Path(__file__).resolve().parents[1] / "reports" / "lab"


def one(pattern):
    hits = sorted(LAB.glob(pattern))
    if not hits:
        pytest.skip(f"no lab report matching {pattern}")
    return hits[0]


def test_awr_html_parses_the_sections_a_dba_reads_first():
    d = awr.parse(one("hard_parse-2*/awr.html"))
    assert d["source"] == "AWR"
    assert d["load_profile"]["Hard parses (SQL)"]["per_sec"] > 1000
    assert d["top_events"][0]["event"] in ("DB CPU", "resmgr:cpu quantum")
    assert {"elapsed", "cpu", "gets"} <= set(d["top_sql"])


def test_statspack_parses_into_the_same_shape():
    d = statspack.parse(one("hard_parse-statspack-*/statspack.txt"))
    assert d["source"] == "Statspack"
    assert d["load_profile"]["Hard parses (SQL)"]["per_sec"] > 1000
    assert d["window"]["elapsed_min"] > 0
    assert d["top_events"] and d["top_events"][0]["event"] == "CPU time"


def test_statspack_sql_text_drops_client_module_lines():
    d = statspack.parse(one("full_scan-statspack-*/statspack.txt"))
    texts = [s["text"] for k in d["top_sql"].values() for s in k]
    assert texts and not any("Module:" in t for t in texts)


@pytest.mark.parametrize("fault", ["hard_parse", "full_scan", "row_lock", "commit_storm", "seq_nocache", "cpu_sql"])
def test_rules_rank_the_injected_cause_first(fault):
    d = awr.parse(one(f"{fault}-2*/awr.html"))
    assert rules.check(d)[0]["cause"] == fault


def test_a_percentage_of_an_idle_database_is_not_a_finding():
    # the healthy run commits after every row, so log file sync is most of its (tiny) DB time
    found = rules.check(awr.parse(one("healthy-2*/awr.html")))
    assert found[0]["cause"] == "healthy"
    assert all(f["score"] < review.SIGNIFICANT for f in found if f["cause"] != "healthy")


def test_numbers_the_model_was_not_given_are_caught():
    allowed = review.allowed_numbers({"hard_parses_per_sec": 3970.6, "hard_parse_pct": 98.9})
    assert review.unsupported("98.9% of parses are hard (3970.6 per second)", allowed) == []
    assert review.unsupported("3,970.6 per second", allowed) == []      # thousands separator
    assert review.unsupported("about 3971 per second", allowed) == []   # rounding of a given number
    assert review.unsupported("this costs 42.5% of DB time", allowed) == [42.5]
    assert review.unsupported("first do 2 things", allowed) == []      # small counts are prose


@pytest.mark.parametrize("fault", ["hard_parse", "full_scan", "row_lock", "commit_storm", "seq_nocache", "cpu_sql", "healthy"])
def test_awr_text_gives_the_same_findings_as_awr_html(fault):
    run = one(f"{fault}-2*/awr.txt").parent
    html = [(f["cause"], f["score"]) for f in rules.check(awr.parse(run / "awr.html"))]
    txt = [(f["cause"], f["score"]) for f in rules.check(text.parse(run / "awr.txt"))]
    assert txt == html


def test_a_pasted_top_events_section_is_enough(tmp_path):
    # what people paste into a forum: one section, 11g layout, two-word wait class
    p = tmp_path / "paste.txt"
    p.write_text("Top 5 Timed Foreground Events\n~~~~~~~~~~~~~~~~~~~~~~~~~~~~~\n"
                 "                                                           Avg\n"
                 "                                                          wait   % DB\n"
                 "Event                                 Waits     Time(s)   (ms)   time Wait Class\n"
                 "------------------------------ ------------ ----------- ------ ------ ----------\n"
                 "direct path read                  3,464,056       6,593      2   33.5 User I/O\n"
                 "DB CPU                                            3,503          17.8\n")
    d = text.parse(p)
    assert [e["event"] for e in d["top_events"]] == ["direct path read", "DB CPU"]
    assert d["top_events"][0]["wait_class"] == "User I/O"
    found = rules.check(d)
    assert found[0]["cause"] == "full_scan"  # no DB time in the paste: judged on the percentages alone


def test_sqlplus_scientific_notation_is_a_number():
    assert text._f("6.50139E+08") == 650139000.0


def test_triage_prompt_is_compact_json_with_the_label_list():
    from second_opinion import triage
    d = statspack.parse(one("hard_parse-statspack-*/statspack.txt"))
    p = triage.prompt(triage.digest(d))
    assert "hard_parse = literal SQL" in p and '"ranked_causes"' in p
    assert len(p) < 12000  # fits the 16K context the tuned model was trained with


@pytest.mark.parametrize("text,want", [
    ('{"ranked_causes": ["row_lock", "hard_parse"], "evidence": "x"}', ["row_lock", "hard_parse"]),
    ('noise {"ranked_causes": ["made_up", "full_scan"]} noise', ["full_scan"]),
    ("not json at all", None),
])
def test_triage_answer_keeps_only_known_labels(text, want):
    from second_opinion import triage
    assert triage.answer(text) == want
