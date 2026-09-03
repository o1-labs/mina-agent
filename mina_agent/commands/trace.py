"""Trajectory evidence for a past run."""
import os
import re

import typer

from .. import trajectory


def trace(log: str = typer.Argument(..., help="A harness/state/logs/*.jsonl run log.")):
    """Trajectory evidence for a past run, from its log.

    Tool inventory (was Bash present?), ordered tool calls with results, hook
    firings, permission denials, and tests_for versus the test actually run.
    Writes <log>.summary.md beside the log.
    """
    traj = trajectory.load(log)
    name = re.sub(r"^\d{8}T\d{6}Z-", "", os.path.splitext(os.path.basename(log))[0])
    md = traj.summary_md(name, log)
    out = os.path.splitext(log)[0] + ".summary.md"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(md)
    print(f"summary written to {out}")
