"""LoRA fine-tune of Qwen3-8B on Tinker.

    .venv-tinker/bin/python lab/tune/train.py --dry-run          # tokens and estimated cost, no API calls
    .venv-tinker/bin/python lab/tune/train.py --max-steps 1      # one step, to check the real charge
    .venv-tinker/bin/python lab/tune/train.py --epochs 3

Needs TINKER_API_KEY in the environment. Prints validation loss after every epoch and saves a
sampler checkpoint per epoch; the paths go to results/tune-run.json for evaluate.py.
"""

import argparse
import json
import random
import time
from pathlib import Path

import tinker
from tinker_cookbook import renderers
from tinker_cookbook.supervised.data import conversation_to_datum
from tinker_cookbook.tokenizer_utils import get_tokenizer

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "tune"
BASE = "Qwen/Qwen3-8B"
RENDERER = "qwen3_disable_thinking"
TRAIN_PRICE = 0.44  # USD per million tokens, Qwen3-8B, Tinker pricing page, Oct 2026


def rows(name):
    return [json.loads(line) for line in open(DATA / f"{name}.jsonl")]


def datums(rs, renderer):
    return [conversation_to_datum(r["messages"], renderer, max_length=8192,
                                  train_on_what=renderers.TrainOnWhat.LAST_ASSISTANT_MESSAGE) for r in rs]


def mean_nll(results, batch):
    """Weighted mean NLL over the answer tokens."""
    total = weight = 0.0
    for out, d in zip(results.loss_fn_outputs, batch):
        lp = out["logprobs"].to_torch()
        w = d.loss_fn_inputs["weights"].to_torch()
        total += float(-(lp * w).sum())
        weight += float(w.sum())
    return total / max(weight, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--lr", type=float, default=None)
    ap.add_argument("--max-steps", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--name", default="so")
    a = ap.parse_args()

    tok = get_tokenizer(BASE)
    renderer = renderers.get_renderer(RENDERER, tok)
    train, val = rows("train"), rows("val")
    dtrain, dval = datums(train, renderer), datums(val, renderer)
    ntok = sum(d.model_input.length for d in dtrain)
    steps_per_epoch = (len(dtrain) + a.batch - 1) // a.batch
    steps = min(a.max_steps or 10**9, steps_per_epoch * a.epochs)
    billed = ntok * min(a.epochs, steps / steps_per_epoch) + sum(d.model_input.length for d in dval) * a.epochs
    print(f"train {len(dtrain)} examples, {ntok:,} tokens/epoch; val {len(dval)}; {steps} steps; "
          f"~{billed / 1e6:.2f}M tokens billed, ~${billed / 1e6 * TRAIN_PRICE:.2f} at ${TRAIN_PRICE}/M")
    if a.dry_run:
        return

    if a.lr is None:
        from tinker_cookbook.hyperparam_utils import get_lr
        a.lr = get_lr(BASE)
    print(f"lr {a.lr}")
    service = tinker.ServiceClient()
    tc = service.create_lora_training_client(base_model=BASE, rank=a.rank, seed=0)
    log = {"base": BASE, "renderer": RENDERER, "rank": a.rank, "lr": a.lr, "batch": a.batch, "epochs": [],
           "train_examples": len(dtrain), "train_tokens_per_epoch": ntok}
    step, rng = 0, random.Random(0)
    for ep in range(a.epochs):
        order = list(range(len(dtrain)))
        rng.shuffle(order)
        losses = []
        for i in range(0, len(order), a.batch):
            if a.max_steps and step >= a.max_steps:
                break
            batch = [dtrain[j] for j in order[i:i + a.batch]]
            frac = step / max(steps, 1)
            lr = a.lr * (1 - frac)  # linear decay
            fb = tc.forward_backward(batch, "cross_entropy")
            opt = tc.optim_step(tinker.AdamParams(learning_rate=lr))
            losses.append(mean_nll(fb.result(), batch))
            opt.result()
            step += 1
            if step % 5 == 0 or step == 1:
                print(f"ep {ep} step {step}/{steps} loss {losses[-1]:.4f}", flush=True)
        vl = []
        for i in range(0, len(dval), a.batch):
            batch = dval[i:i + a.batch]
            vl.append(mean_nll(tc.forward(batch, "cross_entropy").result(), batch))
        path = tc.save_weights_for_sampler(name=f"{a.name}-ep{ep}").result().path
        entry = {"epoch": ep, "steps": step, "train_loss": sum(losses) / max(len(losses), 1),
                 "val_loss": sum(vl) / max(len(vl), 1), "sampler_path": path, "time": time.strftime("%H:%M:%S")}
        log["epochs"].append(entry)
        print(json.dumps(entry), flush=True)
        (ROOT / "results").mkdir(exist_ok=True)
        (ROOT / "results" / f"tune-run-{a.name}.json").write_text(json.dumps(log, indent=1))
        if a.max_steps and step >= a.max_steps:
            break


if __name__ == "__main__":
    main()
