# Second Opinion

A first-pass review of an Oracle AWR or Statspack report, run on your own machine with a local open model (Gemma 4 via Ollama). Nothing about the report leaves the box.

**Status:** work in progress for the DEV Hacktoberfest Week 2 challenge. Results and the full write-up are still to come.

## How it works

1. **Parse.** `second_opinion/awr.py` reads an AWR HTML report and `second_opinion/statspack.py` reads a Statspack text report. Both produce the same small dict: load profile, top events, time model, top SQL and segments. A raw AWR report is about 18k tokens; the dict is a fraction of that.
2. **Rules.** `second_opinion/rules.py` scores known causes (hard parsing, full scans, row locks, commit storms, uncached sequences, CPU-heavy SQL, CPU caps). Anything below a materiality gate counts as healthy.
3. **Gemma 4.** `second_opinion/review.py` asks the model for a ranked diagnosis and a short note for the client.
4. **Number check.** Any number in the model's answer that is not in the data it was given is rejected. The model gets one retry.

Statspack is first-class here. Standard Edition shops have no Diagnostics Pack, so they have no AWR and no ADDM.

## Run

```
pip install -e .            # add [tracing] for optional Sentry spans
ollama pull gemma4:e4b
python -m second_opinion path/to/awr.html     # or statspack.txt
python -m second_opinion report.html --no-gemma   # rules only
```

## Lab

`lab/faults.py` injects known faults into a disposable Oracle container and saves the AWR, ADDM or Statspack report for each run. `lab/score.py` then checks whether ADDM, raw Gemma, the rules and Second Opinion each named the injected cause. Runs in `reports/lab` were used for development. Runs in `reports/test` are held out.

## Tracing

Sentry is optional and off unless `SENTRY_DSN` is set. When it is on, only span names, durations, the model name and token counts are sent. No prompts, no report contents and no PII. See `second_opinion/tracing.py`.

## License

MIT
