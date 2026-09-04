"""Bug reports about the harness itself: an evidence bundle and, when gh is
available, the issue on github.com/o1-labs/mina-agent.

GitHub's API takes no file attachments, so the bundle stays on disk (a
directory plus a zip of it in the system temp dir) and the issue body says
where it is; the browser can attach it by drag-and-drop.
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .model import to_json

ISSUES_REPO = "o1-labs/mina-agent"
NEW_ISSUE_URL = f"https://github.com/{ISSUES_REPO}/issues/new"


@dataclass(frozen=True, slots=True)
class Bundle:
    directory: str
    zip: str
    files: tuple[str, ...]
    size_bytes: int


def _git_rev(path) -> str | None:
    r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None


def _doctor_text(bin_path) -> str:
    """`mina-agent doctor` as plain text (rich honours NO_COLOR and COLUMNS)."""
    env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "140", "TERM": "dumb"}
    r = subprocess.run([bin_path, "doctor"], capture_output=True, text=True, env=env)
    return r.stdout + r.stderr


def environment(env) -> dict:
    """Facts a report needs: harness and Mina commits, toolchain, platform."""
    import platform
    return {"harness_commit": _git_rev(paths.HARNESS), "mina_commit": _git_rev(env.repo),
            "mina_branch": subprocess.run(["git", "-C", env.repo, "branch", "--show-current"],
                                          capture_output=True, text=True).stdout.strip(),
            "mode": str(env.mode), "activated": env.activated, "dune": env.dune_version, "ocaml": env.ocaml,
            "platform": platform.platform(), "python": platform.python_version(),
            "warnings": list(env.warnings)}


def bundle(env, *, runs: int = 2, doctor_text=None) -> Bundle:
    """Collect evidence under <tmp>/mina-agent-bug-<ts>/ and zip it."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(tempfile.gettempdir()) / f"mina-agent-bug-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    def put(name: str, text: str):
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text, encoding="utf-8")
        files.append(name)

    put("environment.json", json.dumps(environment(env), indent=1))
    if doctor_text is None:
        from .agent import mina_agent_bin
        doctor_text = _doctor_text(mina_agent_bin())
    put("doctor.txt", doctor_text)
    logs = paths.state_dir() / "logs"
    lint = logs / "lint.jsonl"
    if lint.exists():
        put("lint.jsonl", "\n".join(lint.read_text().splitlines()[-200:]) + "\n")
    run_logs = sorted(logs.glob("*-*.jsonl"), key=lambda p: p.stat().st_mtime)[-runs:] if logs.exists() else []
    if run_logs:
        (out / "runs").mkdir(exist_ok=True)
    for p in run_logs:
        for src in (p, p.with_suffix(".summary.md")):
            if src.exists():
                shutil.copyfile(src, out / "runs" / src.name)
                files.append(f"runs/{src.name}")
    profile = paths.state_dir() / "profile"
    if (profile / "session.json").exists():
        s = json.loads((profile / "session.json").read_text())
        s.pop("injected", None)          # original dune bytes are not evidence
        put("profile/session.json", json.dumps(s, indent=1))
        for p in sorted(profile.glob("*.log"), key=lambda p: p.stat().st_mtime)[-2:]:
            put(f"profile/{p.name}", "\n".join(p.read_text(errors="replace").splitlines()[-400:]) + "\n")
    z = out.with_suffix(".zip")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:
            zf.write(out / name, arcname=f"{out.name}/{name}")
    return Bundle(str(out), str(z), tuple(files), z.stat().st_size)


def gh_status() -> tuple[str | None, str]:
    """(gh path or None, reason)."""
    gh = shutil.which("gh")
    if not gh:
        return None, "gh is not installed"
    r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True)
    return (gh, "authenticated") if r.returncode == 0 else (None, "gh is not authenticated (gh auth login)")


def file_issue(title: str, body: str, bundle_zip: str | None = None) -> dict:
    """Create the issue with gh, or save the draft when gh cannot."""
    if bundle_zip:
        body = body.rstrip() + (f"\n\n---\nEvidence bundle on the reporter's machine: `{bundle_zip}` "
                                "(GitHub has no upload API; attach it by dragging onto this issue).\n")
    gh, why = gh_status()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    draft = Path(tempfile.gettempdir()) / f"mina-agent-bug-{stamp}.md"
    draft.write_text(f"# {title}\n\n{body}", encoding="utf-8")
    base = {"title": title, "draft": str(draft), "bundle": bundle_zip, "new_issue_url": NEW_ISSUE_URL}
    if not gh:
        return {**base, "filed": False, "reason": why}
    r = subprocess.run([gh, "issue", "create", "--repo", ISSUES_REPO, "--title", title, "--body-file", str(draft)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {**base, "filed": False, "reason": (r.stderr or r.stdout).strip()[-500:]}
    return {**base, "filed": True, "url": r.stdout.strip().splitlines()[-1]}


def bundle_json(b: Bundle) -> dict:
    return to_json(b)
