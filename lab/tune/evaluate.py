"""Score the base and the tuned Qwen3-8B the same way: sampled on Tinker, same renderer, thinking off.

    .venv-tinker/bin/python lab/tune/evaluate.py --set val --model base
    .venv-tinker/bin/python lab/tune/evaluate.py --set val --model tinker://.../sampler_weights/so-ep2
    .venv-tinker/bin/python lab/tune/evaluate.py --set test --model base --model tinker://...   # once

Metrics per run: pass (same rule as lab/score.py: the cause in the top 2, both causes in the top 3),
top-1 correct, false causes (labels named that weren't injected, cpu_cap not counted), invalid JSON.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import tinker
from tinker_cookbook import renderers
from tinker_cookbook.tokenizer_utils import get_tokenizer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data  # noqa: E402

sys.path.insert(0, str(data.ROOT / "lab"))
from score import passed  # noqa: E402

BASE = "Qwen/Qwen3-8B"
RENDERER = "qwen3_disable_thinking"
SAMPLE_PRICE = 0.60  # USD per million tokens (Tinker pricing, Qwen3-8B; prefill billed separately)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="val", choices=["val", "test", "train"])
    ap.add_argument("--model", action="append", required=True, help="'base' or a tinker:// sampler path")
    ap.add_argument("--label", action="append", help="name for each --model in the results file")
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(data.ROOT / "data" / "tune" / f"{a.set}.jsonl")]
    tok = get_tokenizer(BASE)
    renderer = renderers.get_renderer(RENDERER, tok)
    service = tinker.ServiceClient()
    params = tinker.SamplingParams(max_tokens=300, temperature=0.0, stop=renderer.get_stop_sequences())
    labels = a.label or ["base" if m == "base" else m.rsplit("/", 1)[-1] for m in a.model]

    for model, label in zip(a.model, labels):
        sc = (service.create_sampling_client(base_model=BASE) if model == "base"
              else service.create_sampling_client(model_path=model))

        # the tokenizer isn't thread-safe: render and parse here, let Tinker run the samples concurrently
        prompts = [renderer.build_generation_prompt([r["messages"][0]]) for r in rows]
        t0 = time.time()
        futures = [sc.sample(prompt=p, num_samples=1, sampling_params=params) for p in prompts]
        out = []
        for r, prompt, fut in zip(rows, prompts, futures):
            res = fut.result()
            msg, _ = renderer.parse_response(res.sequences[0].tokens)
            text = msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"])
            got = data.answer(text)
            truth = r["causes"]
            out.append({"run": r["run"], "kind": r["kind"], "truth": truth, "got": got, "raw": text,
                        "pass": got is not None and passed(truth, got),
                        "top1": got is not None and got[0] in truth,
                        "false_causes": 0 if got is None else len([c for c in got if c not in truth and c != "cpu_cap"]),
                        "invalid": got is None,
                        "prompt_tokens": prompt.length, "output_tokens": len(res.sequences[0].tokens)})
        wall = round(time.time() - t0, 1)
        n = len(out)
        summary = {"model": model, "label": label, "set": a.set, "n": n,
                   "pass": sum(o["pass"] for o in out), "top1": sum(o["top1"] for o in out),
                   "false_causes": sum(o["false_causes"] for o in out), "invalid": sum(o["invalid"] for o in out), "wall_seconds": wall,
                   "by_kind": {k: f"{sum(o['pass'] for o in out if o['kind'] == k)}/{sum(o['kind'] == k for o in out)}"
                               for k in sorted({o['kind'] for o in out})}}
        for o in out:
            print(f"{label:12} {o['run'][:42]:42} {'PASS' if o['pass'] else 'fail'} truth={o['truth']} got={o['got']}")
        print(json.dumps(summary))
        (data.ROOT / "results").mkdir(exist_ok=True)
        (data.ROOT / "results" / f"tune-{a.set}-{label}.json").write_text(json.dumps({"summary": summary, "runs": out}, indent=1))


if __name__ == "__main__":
    main()
