"""Run a Tinker LoRA locally in Ollama, merged into the stock qwen3:8b.

    tinker checkpoint download tinker://.../sampler_weights/so-ep2 --output models/
    .venv-tinker/bin/python lab/tune/export.py models/<downloaded dir> --name second-opinion-qwen3

Steps: Tinker names the output-layer adapter 'unembed_tokens', which Hugging Face Qwen3 (and so
llama.cpp's converter) calls 'lm_head', so rename it; convert the adapter to GGUF; merge it into the
GGUF that Ollama already has for qwen3:8b; quantize to Q8_0; register it with qwen3:8b's template.

What I saw on the way (llama.cpp build of May 2025, Ollama 0.35):
- Ollama no longer accepts ADAPTER in a Modelfile.
- Merging into the Q4_K_M blob and quantizing back to Q4_K_M lost the tuning: the model went back
  to naming three causes per report, like the base model. Stopping at Q8_0 keeps it.
- A base converted from the Hugging Face weights with this llama.cpp build (bf16 or f16) gave
  degenerate, repeating output once the adapter was merged, although the merged weights matched
  B @ A and the same merge in transformers answered correctly. So the base here is Ollama's blob.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from safetensors.torch import load_file, save_file

LLAMA_CPP = Path(os.environ.get("LLAMA_CPP", "/home/yaroub/Utility_dont_remove/llama.cpp"))


def ollama_modelfile(model):
    return subprocess.run(["ollama", "show", model, "--modelfile"], check=True, capture_output=True, text=True).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("adapter_dir")
    ap.add_argument("--name", default="second-opinion-qwen3")
    ap.add_argument("--base-model", default="qwen3:8b", help="the Ollama model to merge into")
    a = ap.parse_args()
    base_mf = ollama_modelfile(a.base_model)
    base = next(ln.split(None, 1)[1] for ln in base_mf.splitlines() if ln.startswith("FROM "))

    src = Path(a.adapter_dir)
    peft = src.parent / (src.name + "-peft")
    peft.mkdir(exist_ok=True)
    w = load_file(src / "adapter_model.safetensors")
    w = {k.replace("model.model.unembed_tokens", "model.lm_head"): v for k, v in w.items()}
    save_file(w, peft / "adapter_model.safetensors")
    cfg = json.loads((src / "adapter_config.json").read_text())
    cfg["base_model_name_or_path"] = "Qwen/Qwen3-8B"
    (peft / "adapter_config.json").write_text(json.dumps(cfg, indent=1))

    lora = src.parent / f"{a.name}-lora.gguf"
    env = dict(os.environ, PYTHONPATH=str(LLAMA_CPP / "gguf-py"))
    subprocess.run([sys.executable, str(LLAMA_CPP / "convert_lora_to_gguf.py"), "--base-model-id", "Qwen/Qwen3-8B",
                    "--outtype", "f16", "--outfile", str(lora), str(peft)], check=True, env=env, capture_output=True)
    shutil.rmtree(peft)
    merged, q8 = src.parent / f"{a.name}-merged.gguf", src.parent / f"{a.name}-q8_0.gguf"
    subprocess.run([str(LLAMA_CPP / "build/bin/llama-export-lora"), "-m", base, "--lora", str(lora), "-o", str(merged),
                    "-t", "8"], check=True, capture_output=True)
    subprocess.run([str(LLAMA_CPP / "build/bin/llama-quantize"), "--allow-requantize", str(merged), str(q8), "Q8_0", "8"],
                   check=True, capture_output=True)
    merged.unlink()
    mf = src.parent / f"{a.name}.Modelfile"
    mf.write_text("\n".join(f"FROM {q8.resolve()}" if ln.startswith("FROM ") else
                            "PARAMETER temperature 0" if ln.startswith("PARAMETER temperature") else ln
                            for ln in base_mf.splitlines()) + "\n")
    subprocess.run(["ollama", "create", a.name, "-f", str(mf)], check=True, capture_output=True)
    print(f"ollama model {a.name} ready ({q8.stat().st_size / 1e9:.1f} GB)")


if __name__ == "__main__":
    main()
