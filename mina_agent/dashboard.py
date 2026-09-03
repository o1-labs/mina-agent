"""Browser log stream for harness runs and the lint gate.

A stdlib HTTP server (no Node, no extra dependency) that reads what the CLI
already writes under harness/state/logs and serves one flat, newest-first
stream of events, each with a one-line summary and the full record behind a
disclosure. A Server-Sent Event fires whenever any log changes.

Sources:  <UTC>-<phase>.jsonl   run logs, interpreted with trajectory.py
          lint.jsonl            every lint / pre-commit decision

Routes:   /              the page (data/dashboard.html)
          /api/events    [{id, ts, source, kind, level, summary, detail}] newest first
          /events        SSE: {"type": "change"} on any log change
"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import paths, trajectory

PAGE = paths.DATA / "dashboard.html"


def _iso(stamp):
    """20260903T205605Z -> 2026-09-03T20:56:05+00:00"""
    m = re.match(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$", stamp or "")
    return f"{m[1]}-{m[2]}-{m[3]}T{m[4]}:{m[5]}:{m[6]}+00:00" if m else ""


def _short(v, n=110):
    return trajectory.short(v, n)


def _run_events(path):
    """Events for one run log, in file order, each carrying a synthetic
    timestamp (run start + line index) so runs interleave sensibly with lint."""
    m = re.match(r"^(\d{8}T\d{6}Z)-(.+)\.jsonl$", path.name)
    start, phase = (m.group(1), m.group(2)) if m else ("", path.stem)
    base = _iso(start)
    traj = trajectory.Trajectory()
    events = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            before = len(traj.calls)
            traj.feed(rec)
            ts = f"{base}#{i:05d}"
            k = rec.get("kind")
            ev = dict(id=f"{path.stem}:{i}", ts=ts, source=path.stem, phase=phase, detail=rec)
            if k == "SystemMessage" and rec.get("subtype") == "init":
                tools = rec.get("data", {}).get("tools") or []
                bash = "Bash" in tools
                events.append({**ev, "kind": "run.start", "level": "bad" if bash else "ok",
                               "summary": f"run started · {phase} · {len([t for t in tools if t.startswith('mcp__mina-harness__')])} harness tools · "
                                          f"bash {'PRESENT' if bash else 'absent'}"})
            elif k == "AssistantMessage":
                for b in rec.get("content") or []:
                    if b.get("block") == "ToolUseBlock":
                        c = traj.by_id.get(b["id"])
                        events.append({**ev, "id": f"{ev['id']}:{b['id']}", "kind": "tool.call", "level": "info",
                                       "summary": "→ " + (traj.progress_line(c).strip() if c else b["name"])})
                    elif b.get("block") == "TextBlock" and b.get("text", "").strip():
                        events.append({**ev, "id": f"{ev['id']}:text", "kind": "assistant", "level": "dim",
                                       "summary": "assistant: " + _short(b["text"].strip().replace("\n", " "), 140)})
            elif k == "UserMessage":
                for b in rec.get("content") or []:
                    if b.get("block") == "ToolResultBlock" and b.get("tool_use_id") in traj.by_id:
                        c = traj.by_id[b["tool_use_id"]]
                        events.append({**ev, "id": f"{ev['id']}:{b['tool_use_id']}", "kind": "tool.result",
                                       "level": "bad" if c["is_error"] else "ok",
                                       "summary": f"← {trajectory.tool_label(c['name'])} " + _short(traj.result_text(c), 120)})
            elif k == "HarnessHook":
                h = traj.hooks[-1] if traj.hooks else {"name": "?", "output": rec.get("output")}
                out = rec.get("output") or {}
                denied = (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
                events.append({**ev, "kind": "hook", "level": "bad" if denied else "ok",
                               "summary": f"hook {h['name']} → {traj.hook_detail(h)}"})
            elif k == "ResultMessage":
                ok = rec.get("subtype") == "success" and not rec.get("is_error")
                events.append({**ev, "kind": "run.end", "level": "ok" if ok else "bad",
                               "summary": f"run finished · {rec.get('subtype')} · {rec.get('num_turns')} turns · "
                                          f"${round(rec.get('total_cost_usd') or 0, 3)} · {round((rec.get('duration_ms') or 0) / 1000)}s"
                                          + (f" · {len(rec.get('permission_denials') or [])} denial(s)" if rec.get("permission_denials") else "")})
    return events


def _lint_events(path):
    events = []
    if not path.exists():
        return events
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            fails = [r["name"] for r in rec["results"] if r["status"] == "fail"]
            fixed = any(r["status"] == "note" and r["name"] == "ocamlformat" for r in rec["results"])
            files = rec["files"] if isinstance(rec["files"], list) else None
            nfiles = len(files) if files is not None else rec["files"]
            verdict = f"BLOCKED {','.join(fails)}" if rec["blocked"] else ("fixed" if fixed else "passed")
            events.append(dict(id=f"lint:{i}", ts=rec["ts"] + "#99999", source="lint", phase="lint",
                               kind="lint", level="bad" if rec["blocked"] else ("info" if fixed else "ok"),
                               summary=f"lint {rec['caller']} ({rec['scope']}) {verdict} · {nfiles} file(s)"
                                       + (" · " + ", ".join(f.split("/")[-1] for f in files[:4]) if files else ""),
                               detail=rec))
    return events


def events(repo, limit=500):
    d = paths.logs_dir(repo)
    out = []
    for p in d.glob("*.jsonl"):
        out.extend(_lint_events(p) if p.name == "lint.jsonl" else _run_events(p))
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:limit]


def _digest(repo):
    d = paths.logs_dir(repo)
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in d.glob("*.jsonl")))


def make_handler(repo):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype, code=200):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/events":
                self._send(json.dumps(events(repo), default=str).encode(), "application/json")
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
                self._send(b'{"error":"not found"}', "application/json", 404)
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
