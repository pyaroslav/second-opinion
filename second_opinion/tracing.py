"""Optional Sentry agent tracing. Off unless SENTRY_DSN is set.

What leaves the machine when it is on: span names, durations, the model name, token counts, which
causes fired and how many numbers failed the check. What never leaves: the report, SQL text, schema
or host names, prompts and the model's answers. That is the whole point of running the model locally,
so the tracing respects it (send_default_pii=False and no prompt/response attributes).
"""

import contextlib
import os

try:
    import sentry_sdk
except ImportError:  # tracing is optional
    sentry_sdk = None

ON = bool(os.environ.get("SENTRY_DSN")) and sentry_sdk is not None

if ON:
    sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=1.0, send_default_pii=False,
                    environment=os.environ.get("SENTRY_ENVIRONMENT", "local"), default_integrations=False)


@contextlib.contextmanager
def span(op: str, name: str, **data):
    if not ON:
        yield _Null()
        return
    parent = sentry_sdk.get_current_span()
    ctx = parent.start_child(op=op, name=name) if parent else sentry_sdk.start_transaction(op=op, name=name)
    with ctx as s:
        for k, v in data.items():
            s.set_data(k, v)
        yield s


class _Null:
    def set_data(self, *a):
        pass


def flush():
    if ON:
        sentry_sdk.flush(timeout=10)
