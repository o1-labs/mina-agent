"""Declared external plugins, cloned into state, loaded only by harness sessions.

manifest.toml:
    [[plugins]]
    name = "adhd"
    url  = "https://github.com/UditAkhourii/adhd"
    ref  = "16dc239"          # commit or tag; `mina-agent init` checks it out

`init` clones each into harness/state/plugins/<name> (gitignored) and pins
the ref; discuss and review pass every synced plugin with --plugin-dir.
Nothing is vendored into the repo and nothing is installed into the user's
Claude configuration, so plain `claude` sessions never see them.
"""
import subprocess
import tomllib
from pathlib import Path

from . import paths


def declared():
    with open(paths.MANIFEST, "rb") as fh:
        return list(tomllib.load(fh).get("plugins", []))


def plugin_root(repo):
    p = paths.state_dir() / "plugins"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _head(d):
    r = _git(d, "rev-parse", "--short", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def sync(repo):
    """Clone or update each declared plugin to its ref. Yields (name, dir, message)."""
    for p in declared():
        d = plugin_root(repo) / p["name"]
        if not (d / ".git").exists():
            r = _git(plugin_root(repo), "clone", "-q", p["url"], p["name"])
            if r.returncode != 0:
                yield p["name"], d, f"clone failed: {r.stderr.strip()[-200:]}"
                continue
        else:
            _git(d, "fetch", "-q", "origin")
        r = _git(d, "checkout", "-q", "--detach", p["ref"])
        if r.returncode != 0:
            yield p["name"], d, f"ref {p['ref']} not found: {r.stderr.strip()[-200:]}"
            continue
        ok = (d / ".claude-plugin" / "plugin.json").exists()
        yield p["name"], d, f"at {_head(d)}" + ("" if ok else " (no .claude-plugin/plugin.json; not a plugin?)")


def dirs(repo):
    """Synced plugin directories, for --plugin-dir. Missing ones are skipped
    (run `mina-agent init`)."""
    out = []
    for p in declared():
        d = plugin_root(repo) / p["name"]
        if (d / ".claude-plugin" / "plugin.json").exists():
            out.append(d)
    return out


def status(repo):
    """(name, ok, detail) per declared plugin, for doctor."""
    for p in declared():
        d = plugin_root(repo) / p["name"]
        if not (d / ".claude-plugin" / "plugin.json").exists():
            yield p["name"], False, f"not synced; run mina-agent init ({p['url']} @ {p['ref']})"
            continue
        head = _head(d) or "?"
        pinned = _git(d, "rev-parse", "--short", p["ref"]).stdout.strip()
        yield p["name"], head == pinned, f"{d} at {head}" + ("" if head == pinned else f", manifest pins {p['ref']}; run mina-agent init")
