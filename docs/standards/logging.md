# Logging & Observability Standard

> Inherited from the netcanon-dev project-scaffold kit and adapted for br0kr; it does not track the kit.
> br0kr's never-break rules are in [AGENTS.md](../../AGENTS.md).

> Doctrine for **br0kr**. The reference implementation that
> encodes these rules is [`br0kr/logging_setup.py`](../../br0kr/logging_setup.py);
> a worked example is in [`examples/logging_usage.py`](../../examples/logging_usage.py).
>
> Language-general doctrine, Python reference. Where a rule names a Python API,
> read it as *the example* — the principle ports to any language.

The goal an operator should be able to assume: **full-fidelity troubleshooting
of the whole system from logs.** When something breaks, the answer should be
*queryable* — not reconstructed by hand across silos with mismatched timestamps.
Everything below serves that goal.

---

## 1. Structured, dual-shape output

There is **one** place that configures logging (`configure_logging()`), installed
once at process start. It is **idempotent** — re-calling it replaces handlers
cleanly (tests, supervised restarts).

- **Default = plain text**, human-readable: `TS [LEVEL] logger: message`.
- **`LOG_FORMAT=json` = JSON lines**, one object per record, newline-delimited.
- **Pre-authentication refusals** (`br0kr.ratelimit`) are WARNING lines, throttled so a flood leaves a trace
  without flooding the log: a full ledger gate at most once a minute (fields `gate`, `slots`, `refused`), and a
  rate limiter's refusals once per window (`limiter`, `refused`). The bundled compose stack passes
  `LOG_LEVEL`/`LOG_FORMAT`/`BR0KR_LOG_FILE` to every broker process (`x-broker-env`). *Known gap:* the
  gateway's HTTP server (uvicorn, started by the MCP SDK) installs its own handlers, so its startup and
  access lines (`Uvicorn running on …`, `"GET /healthz" 200`) stay plain text; a JSON consumer should skip
  lines that do not start with `{` until br0kr routes uvicorn's loggers through `configure_logging()`.
- Both shapes go to **stderr AND a rotating file**. A machine consumes the JSON
  file; a human tails the same file or watches stderr. You never have to choose.
- Every module does `log = logging.getLogger(__name__)` — never the root logger
  directly, never a bespoke print. Levels and handlers are owned centrally.

**Why:** "troubleshoot from logs" needs records a machine can parse without regex
archaeology, while humans still want a readable console. One config seam keeps
format/level/rotation/redaction policy in exactly one place.

---

## 2. Correlation IDs are first-class

Every logical operation — a request, a job, a batch, a background worker — gets a
short **correlation id** (`run_id`), and that id is attached to *every* log line
the operation emits, **including nested calls and spawned workers**.

- Threading is done with **`contextvars`**, not thread-locals: they copy cleanly
  across threads and async, and don't leak state across thread pools.
- `push_context(**fields)` — context manager, for a bounded scope. Nests; inner
  values override outer for the block's duration; unwinds on exception.
- `set_context(**fields)` — one-way merge, for the top of a worker whose whole
  lifetime is one logical scope.
- `generate_correlation_id()` — a short opaque id for a workflow.

```python
with push_context(run_id=generate_correlation_id(), op="reconcile"):
    log.info("start")          # tagged run_id=…, op=reconcile
    do_work()                  # nested logs tagged too
```

The daemon's reconcile tick is one example: each tick runs under
`run_id`, `op="reconcile_tick"` and `tick=<n>`, and each of its steps adds
`step=<name>` (the row's name in `br0kr/tick_steps.py`), so a tick's log lines
can be split by step without parsing messages.

> **Gotcha (load-bearing):** a `threading.Thread` does **not** inherit the
> parent's context by default — it starts empty. A worker function must
> re-establish context on its **first line** (`set_context(run_id=…, worker=…)`).

**Why:** the single hardest troubleshooting question — *"why did operation X
fail?"* — otherwise spans request logs, worker logs, DB logs, and upstream-call
logs with no shared thread. `run_id` is the choke-point that makes one operation
greppable across every surface, and (§6) the correlation label in the aggregator.

**Tie a `run_id` to actuating paths.** Any code path that *changes state*
(writes a row, calls an external mutating API, runs a job) emits at least one
`run_id`-correlated line. For privileged/audited actions, log **who / when /
what — never the credential** (an audit line, names only).

---

## 3. Level discipline

| Level | Use for | Example |
|---|---|---|
| `ERROR` | a failure needing attention; pair with `exc_info=True` to capture the traceback | a request 500'd; a job aborted |
| `WARNING` | degraded-but-continuing; **also the pin for noisy third-party loggers** | a retry; a down host tolerated; `requests`/`urllib3` pinned here |
| `INFO` | lifecycle / connect / result — the **default** operating level | "started", "connected to DB", "job complete in 12.3s" |
| `DEBUG` | per-command / verbose detail, off in normal ops | the SQL it ran; the request body |

- **Default operating level is INFO.** DEBUG is opt-in.
- **Pin third-party noise to WARNING** so INFO stays signal (configure once, in
  `configure_logging`). Verbose dependency chatter at INFO drowns your own lines.
- A consistent `level` is what makes it a useful low-cardinality label (§6) and
  lets ops dial verbosity by config, not code edits (§7).

---

## 4. Logs are a secret surface — redact at the handler

**Hard rule: never log a secret or PII.** This is enforced *structurally*, not by
reviewer vigilance: a `RedactionFilter` sits on **every handler** and scrubs each
record before it is formatted.

Two layers:
1. **Structured keys** — any `extra={...}` / context field whose KEY matches a
   PII/secret denylist (`password`, `*_token`, `api_key`, `authorization`,
   `secret`, `email`, `ssn`, address words, …) has its value replaced with
   `[REDACTED]`, **recursing** into nested dicts/lists. So a careless
   `log.info('saved', extra={'profile': whole_user})` cannot leak.
2. **Free-text values** — secret *value patterns* (API-key prefixes, `Bearer …`
   tokens, sensitive URL query params) are scrubbed from the message string and
   from exception tracebacks, covering `log.info(f'key={k}')` too.

Properties that matter:
- **Over-redaction is preferred to under-redaction.** Logs are diagnostic, not a
  data API — a benign field caught by the denylist is acceptable collateral.
- **Idempotent:** a per-record flag means a record shared by multiple handlers is
  scrubbed once.
- **Defense in depth:** the JSON formatter re-runs redaction on ambient context
  (which lives in a contextvar, *off* the record, so the handler filter can't
  reach it) — and is self-protecting even if some handler lacks the filter.

The denylist is **project-tunable** — add your domain's secret/PII key names in
`logging_setup.py` (`_SENSITIVE_KEY_SUBSTRINGS` / `_SENSITIVE_KEY_EXACT`) and any
value patterns (`_SECRET_VALUE_PATTERNS`). Do **not** rely on the filter as a
license to log secrets — it is a backstop, not a permission.

---

## 5. Bounded + durable

- The file handler is a **rotating** handler: ≈**5 MB × 3–5 files**. A crash-loop
  must not churn the evidence off disk before you can read it.
- A log file path that cannot be created or opened is a **WARNING, not a crash**
  (`log file … is not writable …; logging to stderr only`): the file is a
  convenience on top of stderr, and the containers' root filesystem is read-only
  (review SEC-02), so a stale `BR0KR_LOG_FILE` would otherwise stop every broker
  service at start. In a container, point it at a mounted volume the broker user
  (uid 10001) can write.
- If you run containers, set log rotation as a **global daemon default**
  (e.g. `/etc/docker/daemon.json`: `json-file`, `max-size 10m` × `max-file 5`)
  so every container — current and future — inherits it, rather than a
  per-service block you can forget. *(Gotcha: a Docker daemon **restart**, not a
  reload, is required for log-opts to take effect.)*
- Logs at rest are **retention-bounded** and live inside the **same trust
  boundary** as the secrets they might accidentally carry. A leaked log should
  reveal ops metadata, not credentials (which §4 already keeps out).

---

## 6. Aggregation: the label schema (do this when you centralize logs)

When logs ship to an aggregator *(the example stack here is Loki + Grafana —
swap for stdout, captured by systemd/journald)*, the stream labels are a **closed, structural
allow-list** of **low-cardinality, non-secret** keys:

```
source    # where it came from (service name, internal|device, …)
host      # the emitting host
service   # the logical component
level     # §3
run_id    # §2 — the correlation key, the cross-silo join
```

plus a small number of **domain dimensions** for br0kr (keep them few and
bounded).

- **Never** a high-cardinality value (a full id space, a URL, a body) as a
  **label** — label-indexed stores blow up on cardinality. Put those in the log
  *body* (queryable by content), not the label set.
- **Never** a secret-valued label — §4 scrubs bodies, not labels; a secret in a
  label is an uncaught leak. The allow-list is the guard.
- **Adding a log source is a drop-in**, never a hub edit: a new source = a new
  collector config file shaping to this same schema. The store/collector are
  hubs that *read* drop-ins; you never edit the hub to add a source.

**Degrade path (always keep one):** when the aggregator/collector is down, the
same records are still readable as the local **rotating file** and **stderr**.
The structured file is the source of truth; the aggregator is a convenience pane.

---

## 7. Configuration precedence

Effective **level** and **format** resolve from three sources, highest first:

1. **Environment** — `LOG_LEVEL` (`DEBUG|INFO|WARNING|ERROR`) and `LOG_FORMAT`
   (`json|text`). Wins at process start, for on-the-fly field debugging with no edit. One exception: the
   reconciler daemon re-reads its level each tick from the `log_level` knob (`br0kr/settings.py`), where a
   ledger override set on the Settings page beats the env.
2. **Config / profile** — a persisted app setting.
3. **Default** — `INFO` / `text`.

Surface **which source won** (`level_source`, `format_source`) so a UI/operator
isn't confused when a saved setting "doesn't take effect" because an env var is
shadowing it. An unknown/garbage value falls all the way back to a valid default
— **nothing ever runs silent.**

---

## 8. Querying (with the JSON file + `jq`)

The structured file is directly queryable before you even stand up an aggregator:

```bash
# All errors
jq 'select(.level=="ERROR") | {ts, logger, msg, exc: .exc.type}' app.log

# The full lifecycle of one operation
jq 'select(.run_id == "a1b2c3d4e5f6")' app.log

# Group a numeric extra by a category field
jq -s 'map(select(.cost != null)) | group_by(.op)
  | map({op: .[0].op, total: (map(.cost) | add)})' app.log
```

---

## 9. Checklist (a behaviour change isn't "done" until…)

- [ ] New cross-module / async flow tags its earliest entry point with a
      `run_id` (and re-establishes context inside any spawned thread).
- [ ] Actuating / state-changing paths emit a `run_id`-correlated line; audited
      actions log who/when/what, **never the credential**.
- [ ] No secret or PII is passed in a message or `extra={}` (the filter is a
      backstop, not a license); new secret/PII key names added to the denylist.
- [ ] New aggregator labels are low-cardinality, non-secret, and in the schema.
- [ ] Level chosen per §3; new noisy dependencies pinned to WARNING.
