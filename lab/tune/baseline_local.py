"""Quick check before generating training data: how well does the untuned model do on the task?

    python lab/tune/baseline_local.py                 # qwen3:8b via local Ollama, dev runs
    python lab/tune/baseline_local.py --think         # same, with thinking on
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402

sys.path.insert(0, str(data.ROOT / "lab"))
from score import passed  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--runs", default=str(data.ROOT / "reports" / "lab"))
    ap.add_argument("--think", action="store_true")
    a = ap.parse_args()
    ok = bad_json = n = 0
    for tj in sorted(Path(a.runs).glob("*/truth.json")):
        t = json.loads(tj.read_text())
        causes = t.get("causes", [t["fault"]])
        p = data.prompt(data.digest(data.load(tj.parent)))
        t0 = time.time()
        r = httpx.post("http://127.0.0.1:11434/api/chat", timeout=900, json={
            "model": a.model, "messages": [{"role": "user", "content": p}], "stream": False, "think": a.think,
            "options": {"temperature": 0.0, "num_ctx": 16384}}).json()
        got = data.answer(r["message"]["content"])
        n += 1
        bad_json += got is None
        hit = got is not None and passed(causes, got)
        ok += hit
        print(f"{tj.parent.name[:38]:38} in={r.get('prompt_eval_count')} {time.time() - t0:5.1f}s "
              f"{'PASS' if hit else 'fail'} truth={causes} got={got}", flush=True)
    print(f"{a.model} think={a.think}: {ok}/{n} pass, {bad_json} invalid JSON")


if __name__ == "__main__":
    main()
