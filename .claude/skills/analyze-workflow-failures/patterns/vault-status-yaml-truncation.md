# Pattern: `wait_for_vault_units_idle` crash on truncated `juju status` YAML

## Summary

During the `sunbeam_enable_vault` layer, `products/sunbeam/enable_features.py` polls Vault unit
health with `wait_for_vault_units_idle()`. That function runs `juju status --format yaml`,
strips ANSI codes, and calls `yaml.safe_load()` **with no error handling**. If the captured
status output is **truncated** (ends mid-document), `yaml.safe_load()` raises a
`ScannerError` and the whole layer aborts — even though Vault itself is healthy.

This is a **tooling-robustness bug**, not a Vault deployment failure. The Vault units unseal
successfully and end up `active/idle`; a single short/garbled read of `juju status` is enough
to kill a multi-hour pipeline run with no retry.

## Symptom (in GitHub Actions logs)

```
Traceback (most recent call last):
  File ".../products/sunbeam/enable_features.py", line 766, in <module>
    enable_plugins(node, plugins, env_config)
  File ".../products/sunbeam/enable_features.py", line 724, in enable_plugins
    enable_vault(node, config, manifest)
  File ".../products/sunbeam/enable_features.py", line 452, in enable_vault
    wait_for_vault_units_idle(node)
  File ".../products/sunbeam/enable_features.py", line 387, in wait_for_vault_units_idle
    jst = yaml.safe_load(no_ansi)
  ...
  File ".../yaml/scanner.py", line 318, in remove_possible_simple_key
    raise ScannerError("while scanning a simple key", key.mark, ...)
yaml.scanner.ScannerError: while scanning a simple key
  in "<unicode string>", line NNNN, column 9:
            sin
            ^
could not find expected ':'
```

**Tell-tales:**
- The failing frame is `enable_features.py:387` → `yaml.safe_load(no_ansi)`.
- The traceback passes through `yaml/scanner.py` **`fetch_stream_end`** →
  `remove_possible_simple_key`. This specifically means the parser hit **end-of-stream** with a
  simple key still pending — i.e. the input string *ended mid-line*, so it was **truncated**,
  not just syntactically malformed.
- The error pointer is at the **last** line of the parsed string, usually a partial field such
  as `sin` (the start of a `since:` timestamp) inside the `storage:` / filesystem section,
  which sits near the end of `juju status` YAML.

### Two observed variants (same root cause)

The corrupted `juju status` capture surfaces as **one of two** `ScannerError` messages. **Both
are end-truncation** of the captured string at the *same* unguarded `yaml.safe_load()`
(`enable_features.py:387`) — the only difference is *which character the cut lands on*:

1. **Cut mid-token** → `while scanning a simple key … could not find expected ':'`, traceback
   through **`fetch_stream_end`**. The pointer is the **last** line and is a *partial* token
   (e.g. `sin`, `me`, `m`, `s`). Example: a `since:` line chopped to `sin`.
2. **Cut right after a colon** → `mapping values are not allowed here`. Example: a timestamp
   `since: 02 Aug 2026 01:40:32Z` chopped to `since: 02 Aug 2026 01:40:` — the surviving
   trailing `:` is followed by end-of-line, which YAML treats as a *mapping value indicator* in
   a position where a value already exists, hence "mapping values are not allowed here". The
   caret points at that final colon (e.g. column 35 of `since: 02 Aug 2026 01:40:`).

Both mean the captured `juju status` output was **truncated**; the message is purely an artifact
of where the truncation fell. If you see either message with the `enable_features.py:387` frame,
it is this pattern — do **not** treat variant 2 as a separate "malformed data" bug.

## Why the output gets truncated

The status command runs via `sqa_common.helpers.local("juju status --format yaml -m <model>")`
on the runner (confirmed by the `[localhost]:` DEBUG prefix and per-line DEBUG logging → this
is `run_cmd`'s **"live"** output mode, not an SSH-to-node call).

It is **not** a small fixed buffer (the `out` string accumulates every chunk), **not** juju's
fault, and **not** a detected command failure (a non-zero exit would raise `CalledProcessError`;
here juju exits 0 and prints complete YAML). The truncation is a **confirmed bug in the
`run_cmd` "live" reader** — reproduced live on a real cluster (see "Live reproduction" below).

### Confirmed mechanism (`products/sqa_common/helpers.py`, `run_cmd` live mode)

```python
selector.register(ps.stdout, EVENT_READ)
if ps.stderr is not None:
    selector.register(ps.stderr, EVENT_READ)   # stderr is registered too
while True:
    for key, _ in selector.select(each_timeout):
        data = key.fileobj.read1()             # returns only ONE ~8 KB buffer at a time
        if not data:                           # EOF on THIS fd (e.g. stderr closing at exit)
            break                              # breaks the for → SKIPS the for/else
        ...
        out += data
    else:                                      # runs only if for did NOT break
        ...
        continue                               # normal path: keep draining
    break                                      # a fd hit EOF → stop the whole loop
ps.communicate()                               # ← RETURN VALUE DISCARDED (the dropped tail)
```

Three facts combine:
1. **`read1()` returns at most one buffer (~8 KB)** — a 120 KB `juju status` needs many loop
   iterations to drain.
2. **Both stdout and stderr are registered.** When juju exits, stderr reaches EOF. If that EOF
   lands in the same `select()` ready-set while stdout still has queued bytes, the `for` reads
   one stdout chunk, then hits stderr → `read1()` empty → `if not data: break`. That `break`
   skips the `for/else` and falls straight to the bottom `break`, **ending the whole read loop
   while stdout still has unread data buffered.**
3. **The post-loop `ps.communicate()` drains that remainder but throws it away** (its return
   value is never assigned to `out`). The tail of the YAML is silently lost.

The reader only falls behind (letting the 64 KB pipe fill) when it is **throttled** — in
production by the per-line `logger.debug(line)` writing to a DEBUG handler (the same per-line
status logging visible in the job logs). Faster/unthrottled reads drain the pipe before the
stderr-EOF race and don't truncate — which is exactly why it is intermittent (race), and why
one run gets 15 clean polls and passes while others crash on a single bad poll.

Regardless, the fatal *second* flaw is on the **consumer** side:
`wait_for_vault_units_idle()` treats one `juju status` snapshot as infallible.

## Live reproduction (confirmed on a real cluster)

Reproduced by importing the actual `run_cmd`/`local` from the repo and polling
`juju status --format yaml -m openstack` (≈121 KB / 4518 lines) in a loop:

| Condition | Truncated |
|---|---|
| Reader **unthrottled** (no logging handler) | **0 / 100** |
| Reader **throttled** with DEBUG logging (as in the pipeline) | **58 / 100** |
| Throttled **+ capture the discarded `ps.communicate()` tail** (the fix) | **0 / 60** |

- Truncated lengths were exact buffer boundaries — 118784, 110592, 102400 (all multiples of
  4096) vs. full 121418 — i.e. cut at a read-buffer boundary, mid-token/mid-line
  (`life: alive\n      st`, `size: 16\n      pe`), producing the identical
  `ScannerError: could not find expected ':'` seen in the failed runs.
- The single-line change of appending the `ps.communicate()` remainder to `out` eliminated all
  truncation, pinpointing that discarded return value as the exact byte-drop location.

## Confirming Vault was actually healthy (not the cause)

```bash
# Unseal operations all completed:
grep "Vault unseal operation status" <uuid>/generated/sunbeam/output.log
# -> "completed" for the leader and each follower

# Final unit states at log collection:
grep -i "vault" <uuid>/generated/sunbeam/juju_status_openstack.txt
# -> vault active ; vault/0* vault/1 vault/2 all active/idle
```

## Confirming the truncation point

```bash
# The DEBUG dump of the parsed status ends abruptly, then time jumps to another phase:
grep -n "juju status --format yaml\|since:\|message: Successfully provisioned" \
    <uuid>/generated/sunbeam/output.log | tail -20
# The last logged line of the status blob (e.g. a "message: ..." line) is immediately
# followed by a log entry from a later timestamp / different model.
```

## The fix (diagnosis only — infra is gone)

1. **Make the parse resilient in `wait_for_vault_units_idle()`:** wrap `yaml.safe_load()` in
   `try/except (yaml.YAMLError, subprocess.CalledProcessError)`; on failure, re-run
   `juju status` and retry within the existing poll/deadline loop instead of crashing. The
   sibling function `reset_ubuntu_pro_subscriptions()` in the same file already does exactly
   this — reuse that pattern.
2. **Prefer `--format json`** for machine parsing: `json.loads` fails cleanly and distinguishes
   "truncated/short read" from "genuinely bad data" better than block YAML.
3. **Fix the capture helper (`run_cmd` live mode) — this is the confirmed primary source of the
   truncation.** After the read loop, capture and append the remaining stdout from
   `ps.communicate()` instead of discarding it (verified live to eliminate truncation: 58/100 →
   0/60). Better still, stop hand-rolling the reader (use `ps.communicate()` / a proper drain)
   so an EOF on stderr can't abandon buffered stdout.

## Observed occurrences

All on branch `main`, substrate `tor3-sqa-shared_maas`, failing at `sunbeam_enable_vault`,
`enable_features.py:387`:

| Date | run_id | UUID | Cluster | Variant |
|---|---|---|---|---|
| 2026-08-02 | 30727560164 | `860009dd-a6b7-49b3-97d8-76d6ee2ae32e` | dh1_j8_1 | end-truncation (`fetch_stream_end`, `…sin`) |
| 2026-08-02 | 30722587581 | `01736656-2260-4a32-939c-fe3cfd69ac6e` | dh1_j8_2 | truncation-at-colon (`mapping values not allowed`, `since: … 01:40:`, line 2416) |
| 2026-08-02 | 30719605356 | `28ae892f-ad87-4ed1-8b53-e86568de9951` | dh1_j8_1 | truncation mid-token (`fetch_stream_end`, `…me`, line 2873) |
| 2026-08-02 | 30718323642 | `d69c0f43-eb39-4015-a23b-b2059ce1167a` | dh1_j9_1 | truncation mid-token (`fetch_stream_end`, `…s`, line 3287) |
| 2026-08-02 | 30714702102 | `03b7b6b9-5fc6-458e-a5d0-ef294d597e76` | dh1_j8_2 | truncation mid-token (`fetch_stream_end`, `…m`, line 3441) |
| 2026-08-02 | 30710410274 | `7fe5ba48-d997-4e4d-aecc-9aaf4d73eb5e` | dh1_j9_1 | truncation-at-colon (`mapping values not allowed`, `since: … 19:`, line 2574) |

Recurs frequently across clusters on the same day — all six are the **same end-truncation** of
`juju status` output; the two distinct `ScannerError` messages differ only by where the cut
landed (mid-token vs immediately after a `:`). This confirms a single flaky code path rather
than a one-off or multiple bugs.
