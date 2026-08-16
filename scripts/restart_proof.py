#!/usr/bin/env python3
"""Prove a systemd unit actually restarted, instead of assuming it did.

Why this exists (FAILURE 1, real incident): after a "successful" deploy,
weft-cloud and weft-web had been running for 8h06m. They were serving OLD
CODE FROM MEMORY while the new code sat on disk, unloaded. nginx got
reloaded, so routing-level fixes appeared to work while every
application-level fix silently did not. Verification came back 6/11 and
looked like the fixes had failed; they had simply never loaded. A manual
`sudo systemctl restart weft-cloud weft-web weft-outbox` then gave 11/11.

`systemctl enable --now <unit>` is a no-op when the unit is already active —
it does not restart a running process, it just makes sure one is running.
The old cutover relied on exactly that call and never noticed the process it
"deployed" was the same PID it started with.

The fix: capture each unit's MainPID before the restart, force an actual
`systemctl restart` (not enable --now), capture MainPID again, and fail
loudly — not silently — if it did not change to a new, non-empty, non-zero
value. This module is the decision logic, kept separate from the
`systemctl`/`ssh` plumbing so it can be unit tested without a real VM.

Usage as a library::

    from restart_proof import evaluate_restart
    failures = evaluate_restart(
        before={"weft-cloud.service": "1234", "weft-web.service": "5678"},
        after={"weft-cloud.service": "9999", "weft-web.service": "5678"},
    )
    # -> [RestartFailure(unit="weft-web.service", reason="MainPID unchanged ...")]

Usage as a CLI (called from redeploy-weft.sh / rollback-weft.sh)::

    python3 restart_proof.py before.env after.env

`before.env` / `after.env` are text files of `<unit>=<pid-or-empty>` lines,
one per unit, exactly what `systemctl show -p MainPID --value <unit>` prints
per unit. Prints one FAIL line per unit that did not prove a restart and
exits 1 if any did; prints one OK line per unit and exits 0 if all proved it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RestartFailure:
    unit: str
    reason: str


def _pid_int(raw: "str | None") -> int:
    """MainPID is a decimal string; systemd prints '0' or '' for "not running"."""
    if raw is None:
        return 0
    raw = raw.strip()
    if not raw or not raw.isdigit():
        return 0
    return int(raw)


def evaluate_restart(before: "dict[str, str]", after: "dict[str, str]") -> "list[RestartFailure]":
    """Return one RestartFailure per unit that did not prove it restarted.

    A unit proves a restart when its MainPID is non-zero AFTER, and different
    from its MainPID BEFORE. A unit that was not running before (PID 0, e.g.
    first deploy) only needs a non-zero PID after — there is nothing to
    "differ from".
    """
    failures: list[RestartFailure] = []
    all_units = list(after.keys())
    for unit in all_units:
        before_pid = _pid_int(before.get(unit))
        after_pid = _pid_int(after.get(unit))
        if after_pid == 0:
            failures.append(
                RestartFailure(unit, f"MainPID is 0/empty after restart — {unit} is not running")
            )
            continue
        if before_pid != 0 and after_pid == before_pid:
            failures.append(
                RestartFailure(
                    unit,
                    f"MainPID unchanged (still {after_pid}) — restart did not take effect, "
                    "the unit is still serving the OLD process from memory",
                )
            )
            continue
        # after_pid != 0 and (before_pid == 0 or after_pid != before_pid): proved.

    for unit in before.keys():
        if unit not in after:
            failures.append(RestartFailure(unit, "unit present before but missing from after-snapshot"))

    return failures


def _parse_env_file(path: Path) -> "dict[str, str]":
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        unit, _, pid = line.partition("=")
        result[unit.strip()] = pid.strip()
    return result


def _main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("usage: restart_proof.py <before.env> <after.env>", file=sys.stderr)
        return 2
    before = _parse_env_file(Path(argv[0]))
    after = _parse_env_file(Path(argv[1]))
    if not after:
        print("FAIL: after-snapshot is empty — nothing to prove", file=sys.stderr)
        return 1

    failures = evaluate_restart(before, after)
    for unit in after:
        matches = [f for f in failures if f.unit == unit]
        if matches:
            print(f"  FAIL  {unit}: {matches[0].reason}")
        else:
            print(f"  OK    {unit}: MainPID {before.get(unit, '?')} -> {after.get(unit)}")

    if failures:
        print(f"\nRESTART NOT PROVEN for {len(failures)} unit(s). Do not report this deploy as successful.")
        return 1
    print("\nRestart proven for every unit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
