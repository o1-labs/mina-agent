#!/usr/bin/env python3
"""Run one headless harness phase.

    python3 harness/run.py --phase harness/phases/fix_build_error.md --arg target=src/lib/hex
        [--max-turns N] [--max-budget-usd X] [--model NAME] [--trace] [--dry-run]
    python3 harness/run.py --trace --from harness/logs/<file>.jsonl   # summarize a past run

What it does, in order:
  1. env.detect()/activate(): the activated environment is what `claude` is
     spawned with, so the MCP server, hooks and merlin inherit the switch.
  2. derive.py, so derived.json is current. Banner.
  3. Render the phase prompt ({{arg}} placeholders) and the system-prompt
     addition (tools.facts()).
  4. claude -p with --append-system-prompt, --mcp-config (inline, this
     server only, --strict-mcp-config), --allowedTools/--disallowedTools
     from the phase, --permission-mode, --max-turns, --max-budget-usd,
     --output-format stream-json --verbose --include-hook-events.
     (CLI reference: System Prompt Customization, MCP Configuration,
     Tool Access & Permissions, Permission Modes, Execution Limits & Budget,
     Output Control & Streaming.)
  5. Stream events to harness/logs/<UTC>-<phase>.jsonl; one progress line
     per tool call on the terminal.
  6. Print the final assistant text and a stats line. With --trace, also
     the trajectory evidence (tool inventory, ordered calls, hook firings,
     denials, tests_for vs test) and write it beside the log as .summary.md.
"""
import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import env as envmod  # noqa: E402

LOGS = os.path.join(HERE, "logs")
SERVER = os.path.join(HERE, "server.py")
VENV_PY = os.path.join(HERE, ".venv", "bin", "python")


# --------------------------------------------------------------------------
# phase files
# --------------------------------------------------------------------------

def load_phase(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise SystemExit(f"{path}: expected a '---' front-matter block")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    body = m.group(2).strip()
    lst = lambda k: [x.strip() for x in meta.get(k, "").split(",") if x.strip()]
    return {
        "name": meta.get("name") or os.path.splitext(os.path.basename(path))[0],
        "allowed_tools": lst("allowed_tools"),
        "disallowed_tools": lst("disallowed_tools"),
        "permission_mode": meta.get("permission_mode", "default"),
        "max_turns": int(meta.get("max_turns", 30)),
        "max_budget_usd": float(meta.get("max_budget_usd", 5)),
        "args": lst("args"),
        "body": body,
    }


def render(phase, args):
    missing = [a for a in phase["args"] if a not in args]
    unknown = [a for a in args if a not in phase["args"]]
    if missing or unknown:
        raise SystemExit(f"phase {phase['name']}: missing args {missing}, unknown args {unknown}; "
                         f"declared: {phase['args']}")
    body = phase["body"]
    for k, v in args.items():
        body = body.replace("{{" + k + "}}", v)
    left = re.findall(r"{{\w+}}", body)
    if left:
        raise SystemExit(f"unfilled placeholders: {left}")
    return body


# --------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------

def claude_argv(phase, prompt, system_add, opts):
    mcp = {"mcpServers": {"mina-harness": {"type": "stdio", "command": VENV_PY, "args": [SERVER]}}}
    argv = ["claude", "-p", prompt,
            "--append-system-prompt", system_add,
            "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
            "--permission-mode", phase["permission_mode"],
            "--max-turns", str(opts.max_turns or phase["max_turns"]),
            "--max-budget-usd", str(opts.max_budget_usd or phase["max_budget_usd"]),
            "--output-format", "stream-json", "--verbose", "--include-hook-events"]
    if phase["allowed_tools"]:
        argv += ["--allowedTools"] + phase["allowed_tools"]
    if phase["disallowed_tools"]:
        argv += ["--disallowedTools"] + phase["disallowed_tools"]
    if opts.model:
        argv += ["--model", opts.model]
    return argv


def short(v, n=70):
    s = json.dumps(v) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 3] + "..."


def tool_label(name):
    return name.replace("mcp__mina-harness__", "harness.")


# --------------------------------------------------------------------------
# event stream analysis (works live and on a saved .jsonl)
# --------------------------------------------------------------------------

class Trajectory:
    def __init__(self):
        self.tools_available = None
        self.calls = []        # {id, name, input, result, is_error}
        self.by_id = {}
        self.hooks = []        # {event, name, output}
        self.denials = []
        self.result = None
        self.session_id = None

    def feed(self, ev):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.tools_available = ev.get("tools") or []
            self.session_id = ev.get("session_id")
        elif t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    c = {"id": block.get("id"), "name": block.get("name"),
                         "input": block.get("input") or {}, "result": None, "is_error": False}
                    self.calls.append(c)
                    self.by_id[c["id"]] = c
        elif t == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_result" and block.get("tool_use_id") in self.by_id:
                    c = self.by_id[block["tool_use_id"]]
                    c["result"] = block.get("content")
                    c["is_error"] = bool(block.get("is_error"))
        elif t == "system" and ev.get("subtype") in ("hook_started", "hook_response", "hook_progress"):
            self.hooks.append({"event": ev["subtype"], "name": ev.get("hook_name"), "raw": ev})
        elif t == "result":
            self.result = ev
            self.session_id = ev.get("session_id") or self.session_id
            self.denials = ev.get("permission_denials") or []

    # ---- rendering ----------------------------------------------------

    def result_text(self, c):
        r = c["result"]
        if isinstance(r, list):
            r = " ".join(b.get("text", "") for b in r if isinstance(b, dict))
        if isinstance(r, str):
            try:
                j = json.loads(r)
                if isinstance(j, dict):
                    keys = [k for k in ("ok", "refused", "elapsed_s", "summary_line") if k in j]
                    bits = [f"{k}={j[k]}" for k in keys]
                    if "errors" in j:
                        bits.append(f"errors={len(j['errors'])}")
                    if "candidates" in j:
                        bits.append("candidates=" + ",".join(x["name"] for x in j["candidates"][:3]))
                    if "definition" in j:
                        bits.append(f"definition={short(j['definition'], 50)}")
                    if "type" in j:
                        bits.append(f"type={short(j['type'], 40)}")
                    return " ".join(bits) or short(r)
            except ValueError:
                pass
            return short(r)
        return short(r or "")

    def progress_line(self, c):
        inp = c["input"]
        arg = inp.get("target") or inp.get("path") or inp.get("name") or inp.get("library") \
            or inp.get("file_path") or inp.get("file") or inp.get("pattern") or ""
        return f"  {tool_label(c['name'])} {short(str(arg), 60)}"

    def stats_line(self):
        r = self.result or {}
        return (f"turns={r.get('num_turns')} cost_usd={round(r.get('total_cost_usd') or 0, 3)} "
                f"duration_s={round((r.get('duration_ms') or 0) / 1000)} "
                f"stop={r.get('subtype')} session={self.session_id}")

    def summary_md(self, phase_name, log_path):
        L = [f"# {phase_name} trajectory", "", f"log: `{log_path}`", "",
             "## Tool inventory given to the model"]
        if self.tools_available is None:
            L.append("(no init event found)")
        else:
            has_bash = "Bash" in self.tools_available
            harness = sorted(t for t in self.tools_available if t.startswith("mcp__mina-harness__"))
            other = sorted(t for t in self.tools_available if not t.startswith("mcp__"))
            L.append(f"- Bash present: **{has_bash}**")
            L.append(f"- harness tools ({len(harness)}): " + ", ".join(tool_label(t) for t in harness))
            L.append(f"- built-in tools: " + ", ".join(other))
        L += ["", "## Ordered tool calls", "", "| # | tool | input | result |", "|---|---|---|---|"]
        for i, c in enumerate(self.calls, 1):
            L.append(f"| {i} | {tool_label(c['name'])} | {short(c['input'], 60)} | "
                     f"{'ERROR ' if c['is_error'] else ''}{short(self.result_text(c), 90)} |")
        L += ["", "## Hook firings", ""]
        fired = [h for h in self.hooks if h["event"] == "hook_response"]
        if not fired and not self.hooks:
            L.append("(none recorded)")
        for h in self.hooks:
            raw = h["raw"]
            out = raw.get("output") or ""
            detail = ""
            if h["event"] == "hook_response":
                try:
                    j = json.loads(out)
                    ctx = (j.get("hookSpecificOutput") or {}).get("additionalContext") or ""
                    try:
                        cj = json.loads(ctx)
                        hc = cj.get("harness_check") or {}
                        detail = (f"check {hc.get('alias')} ok={hc.get('ok')} "
                                  f"errors={len(hc.get('errors', []))}")
                        if hc.get("errors"):
                            e0 = hc["errors"][0]
                            detail += f" first={e0['file']}:{e0['line']} {short(e0['message'], 60)}"
                    except ValueError:
                        detail = short(ctx, 100) or short(j.get("systemMessage") or "", 60)
                except ValueError:
                    detail = short(out, 100)
            L.append(f"- {h['event']}: {h['name']} {detail}")
        L += ["", "## Permission denials", ""]
        L += [f"- {d.get('tool_name')} {short(d.get('tool_input'), 100)}" for d in self.denials] or ["(none)"]
        L += ["", "## tests_for vs test", ""]
        tf = [c for c in self.calls if c["name"].endswith("tests_for")]
        te = [c for c in self.calls if c["name"].endswith("__test")]
        for c in tf:
            L.append(f"- tests_for({short(c['input'], 50)}) -> {self.result_text(c)}")
        for c in te:
            L.append(f"- test({c['input'].get('name')}) -> {self.result_text(c)}")
        if not tf and not te:
            L.append("(no test calls)")
        L += ["", "## Stats", "", self.stats_line(), ""]
        return "\n".join(L)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run_phase(opts):
    e = envmod.detect()
    if e.mode == "none":
        sys.stderr.write("run.py: " + "; ".join(e.reasons) + "\n")
        return 3
    aenv = e.activate()
    subprocess.run([sys.executable, os.path.join(HERE, "derive.py")], env=aenv, check=True,
                   stdout=subprocess.DEVNULL)
    import banner
    sys.stdout.write(banner.render(e.to_dict()))

    phase = load_phase(opts.phase)
    args = dict(a.split("=", 1) for a in opts.arg)
    prompt = render(phase, args)
    import tools
    system_add = "\n".join(tools.facts())
    argv = claude_argv(phase, prompt, system_add, opts)

    if opts.dry_run:
        print("\n[dry-run] argv:")
        for a in argv:
            print("   ", short(a, 160))
        print("\n[dry-run] prompt:\n" + prompt)
        print("\n[dry-run] system prompt addition:\n" + system_add)
        return 0

    os.makedirs(LOGS, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(LOGS, f"{stamp}-{phase['name']}.jsonl")
    print(f"\nphase {phase['name']}  args {args}  log {os.path.relpath(log_path, e.repo)}\n")

    traj = Trajectory()
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(argv, env=aenv, cwd=e.repo, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        seen = 0
        for line in proc.stdout:
            log.write(line)
            log.flush()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            traj.feed(ev)
            for c in traj.calls[seen:]:
                print(traj.progress_line(c), flush=True)
            seen = len(traj.calls)
        proc.wait()
        err = proc.stderr.read()
    if err.strip():
        sys.stderr.write(err[-2000:])
    return finish(traj, phase["name"], log_path, opts.trace, proc.returncode)


def summarize_from(opts):
    traj = Trajectory()
    with open(opts.from_log, encoding="utf-8") as fh:
        for line in fh:
            try:
                traj.feed(json.loads(line))
            except ValueError:
                continue
    name = re.sub(r"^\d{8}T\d{6}Z-", "", os.path.splitext(os.path.basename(opts.from_log))[0])
    return finish(traj, name, opts.from_log, True, 0)


def finish(traj, phase_name, log_path, trace, rc):
    r = traj.result or {}
    print("\n=== result ===")
    print((r.get("result") or "(no final assistant text)").strip())
    print("\n" + traj.stats_line())
    if trace:
        md = traj.summary_md(phase_name, log_path)
        out = os.path.splitext(log_path)[0] + ".summary.md"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("\n=== trace ===\n" + md)
        print(f"summary written to {out}")
    return 0 if (rc == 0 and r.get("subtype") == "success") else 1


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", help="phase markdown file")
    ap.add_argument("--arg", action="append", default=[], help="key=value for a {{key}} placeholder")
    ap.add_argument("--max-turns", type=int)
    ap.add_argument("--max-budget-usd", type=float)
    ap.add_argument("--model")
    ap.add_argument("--trace", action="store_true", help="print and save the trajectory evidence")
    ap.add_argument("--dry-run", action="store_true", help="print argv and prompts, run nothing")
    ap.add_argument("--from", dest="from_log", help="summarize an existing .jsonl instead of running")
    opts = ap.parse_args(argv)
    if opts.from_log:
        return summarize_from(opts)
    if not opts.phase:
        ap.error("--phase is required unless --from is given")
    return run_phase(opts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
