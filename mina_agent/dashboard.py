"""Browser dashboard for harness runs and the lint gate.

A stdlib HTTP server (no Node, no extra dependency) that reads what the CLI
already writes under harness/state/logs:
  * <UTC>-<phase>.jsonl   headless run logs, interpreted with trajectory.py
  * lint.jsonl            every lint / pre-commit decision
and pushes a Server-Sent Event whenever any of them changes, so the page
refreshes live while a run is in progress. Runs from other terminals or from
yesterday show the same way, since only the files are read.

Routes:  /            the page (data/dashboard.html)
         /api/runs    run list with stats
         /api/run/ID  one run's trajectory
         /api/lint    lint history
         /events      SSE: {"type": "change"} on any log change
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from . import paths, trajectory

PAGE = paths.DATA / "dashboard.html"


def _runs(repo):
    d = paths.logs_dir(repo)
    out = []
    for p in sorted(d.glob("*.jsonl"), reverse=True):
        if p.name == "lint.jsonl":
            continue
        m = re.match(r"^(\d{8}T\d{6}Z)-(.+)\.jsonl$", p.name)
        traj = trajectory.load(p)
        r = traj.result or {}
        out.append({"id": p.stem, "phase": m.group(2) if m else p.stem, "started": m.group(1) if m else "",
                    "calls": len(traj.calls), "turns": r.get("num_turns"),
                    "cost_usd": round(r.get("total_cost_usd") or 0, 3),
                    "duration_s": round((r.get("duration_ms") or 0) / 1000),
                    "status": r.get("subtype") or "running",
                    "bash_present": (traj.tools_available is not None and "Bash" in traj.tools_available)})
    return out


def _run(repo, run_id):
    p = paths.logs_dir(repo) / f"{run_id}.jsonl"
    if not p.exists() or "/" in run_id:
        return None
    traj = trajectory.load(p)
    r = traj.result or {}
    harness = sorted(t for t in (traj.tools_available or []) if t.startswith("mcp__mina-harness__"))
    builtin = sorted(t for t in (traj.tools_available or []) if not t.startswith("mcp__"))
    return {
        "id": run_id,
        "inventory": None if traj.tools_available is None else {
            "bash_present": "Bash" in traj.tools_available,
            "harness": [trajectory.tool_label(t) for t in harness], "builtin": builtin},
        "calls": [{"n": i, "tool": trajectory.tool_label(c["name"]), "input": c["input"],
                   "result": traj.result_text(c), "error": c["is_error"]}
                  for i, c in enumerate(traj.calls, 1)],
        "hooks": [{"name": h["name"], "input": h.get("input"), "detail": traj.hook_detail(h)} for h in traj.hooks],
        "denials": r.get("permission_denials") or [],
        "result_text": r.get("result"), "stats": traj.stats_line(),
        "status": r.get("subtype") or "running", "cost_usd": r.get("total_cost_usd"),
        "turns": r.get("num_turns"), "duration_s": round((r.get("duration_ms") or 0) / 1000),
    }


def _lint(repo, n=50):
    from . import lint
    return lint.history(repo, n)


def _digest(repo):
    d = paths.logs_dir(repo)
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in d.glob("*.jsonl")))


def make_handler(repo):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(self.path.split("?", 1)[0])
            if path == "/":
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/runs":
                self._json(_runs(repo))
            elif path.startswith("/api/run/"):
                r = _run(repo, path[len("/api/run/"):])
                self._json(r if r else {"error": "no such run"}, 200 if r else 404)
            elif path == "/api/lint":
                self._json(_lint(repo))
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                last = None
                try:
                    while True:
                        cur = _digest(repo)
                        if cur != last:
                            self.wfile.write(b'data: {"type":"change"}\n\n')
                            last = cur
                        else:
                            self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(1)
                except (BrokenPipeError, ConnectionResetError):
                    return
            else:
                self._json({"error": "not found"}, 404)
    return H


def serve(repo, port=8765):
    """Bind the first free port from `port`; returns (server, port)."""
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), make_handler(repo))
            srv.daemon_threads = True
            return srv, p
        except OSError:
            continue
    raise RuntimeError(f"no free port in {port}..{port + 19}")
