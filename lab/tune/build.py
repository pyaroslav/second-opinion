"""Turn lab runs into chat examples for Tinker.

    python lab/tune/build.py

    data/tune/train.jsonl   90% of reports/train (generated runs)
    data/tune/val.jsonl     the other 10% of reports/train, plus the 14 dev runs (reports/lab)
    data/tune/test.jsonl    the 18 held-out runs (reports/test), for scoring only, never for training

Training labels are the injected causes that reached the report (see data.visible); dev and test
labels are the injected causes as they are, the same truth lab/score.py uses.
"""

import json
import random
from pathlib import Path

import data

OUT = data.ROOT / "data" / "tune"


def example(run: Path, check_visible: bool) -> dict | None:
    t = json.loads((run / "truth.json").read_text())
    causes = t.get("causes", [t["fault"]])
    try:
        d = data.load(run)
    except Exception as e:
        print(f"skip {run.name}: {e}")
        return None
    if check_visible:
        seen = [c for c in causes if data.visible(c, d)]
        if not seen:
            return None
        causes = seen
    kind = "statspack" if (run / "statspack.txt").exists() else "awr"
    return {"run": run.name, "kind": kind, "causes": causes, "injected": t.get("causes", [t["fault"]]),
            "messages": [{"role": "user", "content": data.prompt(data.digest(d))},
                         {"role": "assistant", "content": data.target(causes, d)}]}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    runs = sorted(p.parent for p in (data.ROOT / "reports" / "train").glob("*/truth.json"))
    gen = [e for e in (example(r, True) for r in runs) if e]
    dropped = len(runs) - len(gen)
    relabelled = sum(e["causes"] != e["injected"] for e in gen)
    random.Random(0).shuffle(gen)
    n_val = max(1, len(gen) // 10)
    val = gen[:n_val] + [e for e in (example(p.parent, False) for p in sorted((data.ROOT / "reports" / "lab").glob("*/truth.json"))) if e]
    test = [e for e in (example(p.parent, False) for p in sorted((data.ROOT / "reports" / "test").glob("*/truth.json"))) if e]
    for name, rows in (("train", gen[n_val:]), ("val", val), ("test", test)):
        with open(OUT / f"{name}.jsonl", "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    count = {}
    for e in gen[n_val:]:
        for c in e["causes"]:
            count[c] = count.get(c, 0) + 1
    print(f"generated runs {len(runs)}: kept {len(gen)}, dropped {dropped} (no injected cause visible), "
          f"relabelled {relabelled} (one of two causes not visible)")
    print(f"train {len(gen) - n_val}  val {len(val)}  test {len(test)}")
    print("train labels:", dict(sorted(count.items())))


if __name__ == "__main__":
    main()
