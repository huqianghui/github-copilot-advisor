# ACA cold attempt — incomplete/unverified evidence

Run `df12a3106f4340ec83c01234be5ada5f`.

No final result bytes were exported. The last driver-MI checkpoint records state `running`, terminal_count0 and pending_count0; it contains no submitted question intent. This is not a successful cold sample, not an invented censored request, and not proof of a terminal stop reason.

The sealed plan expired at `2026-09-19T06:41:13.729465+00:00`. A read-only final-blob watcher ended without a complete final payload. ARM later showed the driver container not started/not ready with restartCount4; autostart remains false. No START was repeated and no source or policy fix was applied.

Raw checkpoint: `deploy/out/bench/aca-cold/df12a3106f4340ec83c01234be5ada5f-checkpoint.json`; SHA-256 `914d098f9c7a83dcd8fc1b0bdb8b4301c3afcb7bf2da241b9d6521a8429c6836`.
