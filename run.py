#!/usr/bin/env python3
"""Run one headless harness phase on the Claude Agent SDK.

    python3 harness/run.py --phase harness/phases/fix_build_error.md --arg target=src/lib/hex
        [--max-turns N] [--max-budget-usd X] [--model NAME] [--trace] [--dry-run]
    python3 harness/run.py --trace --from harness/logs/<file>.jsonl   # summarize a past run

Re-executes itself under harness/.venv (claude-agent-sdk lives there), so
plain python3 works.

What it does, in order:
  1. env.detect()/activate(): the activated environment is what the CLI
     subprocess gets, so the MCP server, hooks and merlin inherit the switch.
  2. derive.py, so derived.json is current. Banner.
  3. Render the phase prompt ({{arg}} placeholders) and build ClaudeAgentOptions:
       system_prompt   claude_code preset + tools.facts() appended
       mcp_servers     this server only (strict_mcp_config)
       allowed_tools / disallowed_tools / permission_mode / max_turns /
       max_budget_usd  from the phase file
       disallowed_tools also carries the deny rules from settings.template.json,
                       so the walls hold with setting_sources=["project"]
       hooks           in-process: PostToolUse Edit|Write -> tools.check;
                       PreToolUse Bash -> hooks/pre_bash_guard.offending
       setting_sources ["project"] (CLAUDE.md, no local hooks: one hook path)
  4. Stream typed messages, serialize each to harness/logs/<UTC>-<phase>.jsonl,
     print one progress line per tool call.
  5. Print the final assistant text and a stats line. --trace adds the
     trajectory evidence (tool inventory, ordered calls, hook firings, denials,
     tests_for vs test) and writes it beside the log as .summary.md.
"""
import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
VENV_PY = os.path.join(VENV, "bin", "python")
if os.path.exists(VENV_PY) and os.path.realpath(sys.prefix) != os.path.realpath(VENV):
    os.execv(VENV_PY, [VENV_PY] + sys.argv)

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "hooks"))
import env as envmod  # noqa: E402

LOGS = os.path.join(HERE, "logs")
SERVER = os.path.join(HERE, "server.py")
TEMPLATE = os.path.join(HERE, "settings.template.json")


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
    lst = lambda k: [x.strip() for x in meta.get(k, "").split(",") if x.strip()]
    return {
        "name": meta.get("name") or os.path.splitext(os.path.basename(path))[0],
        "allowed_tools": lst("allowed_tools"),
        "disallowed_tools": lst("disallowed_tools"),
        "permission_mode": meta.get("permission_mode", "default"),
        "max_turns": int(meta.get("max_turns", 30)),
        "max_budget_usd": float(meta.get("max_budget_usd", 5)),
        "args": lst("args"),
        "body": m.group(2).strip(),
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
# in-process hooks (same logic as harness/hooks/*.py, no subprocess)
# --------------------------------------------------------------------------

async def post_edit_check(inp, tool_use_id, ctx):
    path = (inp.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith((".ml", ".mli")):
        return record_hook("PostToolUse", inp.get("tool_name"), inp.get("tool_input"), {})
    import tools
    try:
        r = tools.check(path)
    except Exception as ex:
        return record_hook("PostToolUse", inp.get("tool_name"), inp.get("tool_input"),
                           {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                   "additionalContext": f"harness check skipped for {path}: {ex}"}})
    ctx_json = {"harness_check": {"path": r["path"], "alias": r["alias"], "ok": r["ok"],
                                  "elapsed_s": r["elapsed_s"], "errors": r["errors"],
                                  "warnings": r["warnings"][:10]}}
    if not r["ok"] and not r["errors"]:
        ctx_json["harness_check"]["raw_tail"] = r["raw_tail"]
    return record_hook("PostToolUse", inp.get("tool_name"), inp.get("tool_input"),
                       {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                               "additionalContext": json.dumps(ctx_json)}})


async def pre_bash_guard(inp, tool_use_id, ctx):
    import pre_bash_guard as guard
    hit = guard.offending((inp.get("tool_input") or {}).get("command") or "")
    if not hit:
        return record_hook("PreToolUse", "Bash", inp.get("tool_input"), {})
    sub, word, why = hit
    return record_hook("PreToolUse", "Bash", inp.get("tool_input"),
                       {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                               "permissionDecisionReason":
                                                   f"blocked `{sub}`: `{word}` is not allowed here; {why}"}})


# --------------------------------------------------------------------------
# options
# --------------------------------------------------------------------------

def build_options(phase, system_add, aenv, repo, opts):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    with open(TEMPLATE) as fh:
        deny = json.load(fh)["permissions"]["deny"]
    disallowed = list(dict.fromkeys(phase["disallowed_tools"] + deny))
    return ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_add},
        mcp_servers={"mina-harness": {"type": "stdio", "command": VENV_PY, "args": [SERVER]}},
        strict_mcp_config=True,
        allowed_tools=phase["allowed_tools"],
        disallowed_tools=disallowed,
        permission_mode=phase["permission_mode"],
        max_turns=opts.max_turns or phase["max_turns"],
        max_budget_usd=opts.max_budget_usd or phase["max_budget_usd"],
        model=opts.model,
        cwd=repo,
        env=aenv,
        setting_sources=["project"],
        include_hook_events=True,
        hooks={"PostToolUse": [HookMatcher(matcher="Edit|Write", hooks=[post_edit_check], timeout=600)],
               "PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_bash_guard], timeout=10)]},
        stderr=lambda line: STDERR.append(line),
    )


STDERR = []
LOG = {"fh": None}


def record_hook(event, tool, inp, output):
    """Write a HarnessHook line into the run log from inside a callback, so the
    trace has first-hand evidence of every in-process hook firing."""
    rec = {"kind": "HarnessHook", "event": event, "tool": tool,
           "input": {k: v for k, v in (inp or {}).items() if k in ("file_path", "command")},
           "output": output}
    if LOG["fh"]:
        LOG["fh"].write(json.dumps(rec, default=str) + "\n")
        LOG["fh"].flush()
    if LOG.get("traj"):
        LOG["traj"].feed(json.loads(json.dumps(rec, default=str)))
    return output


def options_summary(o):
    d = {k: getattr(o, k) for k in ("allowed_tools", "disallowed_tools", "permission_mode",
                                    "max_turns", "max_budget_usd", "model", "cwd",
                                    "setting_sources", "strict_mcp_config")}
    d["mcp_servers"] = list(o.mcp_servers)
    d["hooks"] = {k: [m.matcher for m in v] for k, v in (o.hooks or {}).items()}
    d["system_prompt_append_chars"] = len(o.system_prompt.get("append", ""))
    d["env_overrides"] = sorted(k for k, v in o.env.items() if os.environ.get(k) != v)
    return d


# --------------------------------------------------------------------------
# messages -> log lines -> trajectory
# --------------------------------------------------------------------------

def to_record(msg):
    """Serialize an SDK message to a plain dict (the .jsonl line format)."""
    d = {"kind": type(msg).__name__}
    if dataclasses.is_dataclass(msg):
        for f in dataclasses.fields(msg):
            v = getattr(msg, f.name)
            if f.name == "content" and isinstance(v, list):
                v = [{"block": type(b).__name__, **(dataclasses.asdict(b) if dataclasses.is_dataclass(b) else {"value": b})}
                     for b in v]
            d[f.name] = v
    else:
        d["value"] = str(msg)
    return json.loads(json.dumps(d, default=str))


def short(v, n=70):
    s = json.dumps(v) if not isinstance(v, str) else v
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n - 3] + "..."


def tool_label(name):
    return name.replace("mcp__mina-harness__", "harness.")


class Trajectory:
    def __init__(self):
        self.tools_available = None
        self.calls, self.by_id, self.hooks = [], {}, []
        self.result = None

    def feed(self, rec):
        k = rec.get("kind")
        if k == "SystemMessage":
            sub, data = rec.get("subtype"), rec.get("data") or {}
            if sub == "init":
                self.tools_available = data.get("tools") or []
            elif sub in ("hook_started", "hook_response", "hook_progress"):
                self.hooks.append({"event": sub, "name": data.get("hook_name"), "data": data})
        elif k == "AssistantMessage":
            for b in rec.get("content") or []:
                if b.get("block") == "ToolUseBlock":
                    c = {"id": b["id"], "name": b["name"], "input": b.get("input") or {},
                         "result": None, "is_error": False}
                    self.calls.append(c)
                    self.by_id[c["id"]] = c
        elif k == "UserMessage":
            for b in rec.get("content") or []:
                if b.get("block") == "ToolResultBlock" and b.get("tool_use_id") in self.by_id:
                    c = self.by_id[b["tool_use_id"]]
                    c["result"] = b.get("content")
                    c["is_error"] = bool(b.get("is_error"))
        elif k == "HarnessHook":
            self.hooks.append({"event": "hook_response", "name": f"{rec['event']}:{rec['tool']}",
                               "data": {"output": rec.get("output"), "input": rec.get("input")}})
        elif k == "ResultMessage":
            self.result = rec

    # ---- rendering ----------------------------------------------------

    def result_text(self, c):
        r = c["result"]
        if isinstance(r, list):
            r = " ".join(b.get("text", "") for b in r if isinstance(b, dict))
        if isinstance(r, str):
            try:
                j = json.loads(r)
                if isinstance(j, dict):
                    bits = [f"{k}={j[k]}" for k in ("ok", "refused", "elapsed_s", "summary_line") if k in j]
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
        arg = (inp.get("target") or inp.get("path") or inp.get("name") or inp.get("library")
               or inp.get("file_path") or inp.get("file") or inp.get("pattern") or "")
        return f"  {tool_label(c['name'])} {short(str(arg), 60)}"

    def stats_line(self):
        r = self.result or {}
        return (f"turns={r.get('num_turns')} cost_usd={round(r.get('total_cost_usd') or 0, 3)} "
                f"duration_s={round((r.get('duration_ms') or 0) / 1000)} "
                f"stop={r.get('subtype')} session={r.get('session_id')}")

    def hook_detail(self, h):
        out = h["data"].get("output") or ""
        try:
            j = json.loads(out) if isinstance(out, str) else out
            ctx = (j.get("hookSpecificOutput") or {}).get("additionalContext") or ""
            hc = (json.loads(ctx).get("harness_check") or {}) if ctx.startswith("{") else {}
            if hc:
                s = f"check {hc.get('alias')} ok={hc.get('ok')} errors={len(hc.get('errors', []))}"
                if hc.get("errors"):
                    e0 = hc["errors"][0]
                    s += f" first={e0['file']}:{e0['line']} {short(e0['message'], 60)}"
                return s
            reason = (j.get("hookSpecificOutput") or {}).get("permissionDecisionReason")
            return short(reason or ctx or "", 100)
        except (ValueError, AttributeError):
            return short(str(out), 100)

    def summary_md(self, phase_name, log_path):
        L = [f"# {phase_name} trajectory", "", f"log: `{log_path}`", "",
             "## Tool inventory given to the model"]
        if self.tools_available is None:
            L.append("(no init event found)")
        else:
            harness = sorted(t for t in self.tools_available if t.startswith("mcp__mina-harness__"))
            other = sorted(t for t in self.tools_available if not t.startswith("mcp__"))
            L.append(f"- Bash present: **{'Bash' in self.tools_available}**")
            L.append(f"- harness tools ({len(harness)}): " + ", ".join(tool_label(t) for t in harness))
            L.append("- built-in tools: " + ", ".join(other))
        L += ["", "## Ordered tool calls", "", "| # | tool | input | result |", "|---|---|---|---|"]
        for i, c in enumerate(self.calls, 1):
            L.append(f"| {i} | {tool_label(c['name'])} | {short(c['input'], 60)} | "
                     f"{'ERROR ' if c['is_error'] else ''}{short(self.result_text(c), 90)} |")
        L += ["", "## Hook firings", ""]
        L += [f"- {h['name']} {short(h['data'].get('input') or '', 60)} -> "
              f"{self.hook_detail(h) or 'no action'}" for h in self.hooks] or ["(none recorded)"]
        L += ["", "## Permission denials", ""]
        den = (self.result or {}).get("permission_denials") or []
        L += [f"- {d.get('tool_name')} {short(d.get('tool_input'), 100)}" for d in den] or ["(none)"]
        L += ["", "## tests_for vs test", ""]
        tf = [c for c in self.calls if c["name"].endswith("tests_for")]
        te = [c for c in self.calls if c["name"].endswith("__test")]
        L += [f"- tests_for({short(c['input'], 50)}) -> {self.result_text(c)}" for c in tf]
        L += [f"- test({c['input'].get('name')}) -> {self.result_text(c)}" for c in te]
        if not tf and not te:
            L.append("(no test calls)")
        L += ["", "## Stats", "", self.stats_line(), ""]
        return "\n".join(L)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def run_query(prompt, options, log_path, traj):
    from claude_agent_sdk import query
    seen = 0
    with open(log_path, "w", encoding="utf-8") as log:
        LOG["fh"], LOG["traj"] = log, traj
        async for msg in query(prompt=prompt, options=options):
            rec = to_record(msg)
            log.write(json.dumps(rec) + "\n")
            log.flush()
            traj.feed(rec)
            for c in traj.calls[seen:]:
                print(traj.progress_line(c), flush=True)
            seen = len(traj.calls)


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
    options = build_options(phase, system_add, aenv, e.repo, opts)

    if opts.dry_run:
        print("\n[dry-run] options:\n" + json.dumps(options_summary(options), indent=1, default=str))
        print("\n[dry-run] prompt:\n" + prompt)
        print("\n[dry-run] system prompt addition:\n" + system_add)
        return 0

    os.makedirs(LOGS, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = os.path.join(LOGS, f"{stamp}-{phase['name']}.jsonl")
    print(f"\nphase {phase['name']}  args {args}  log {os.path.relpath(log_path, e.repo)}\n")
    traj = Trajectory()
    asyncio.run(run_query(prompt, options, log_path, traj))
    if STDERR:
        sys.stderr.write("\n".join(STDERR[-20:]) + "\n")
    return finish(traj, phase["name"], log_path, opts.trace)


def summarize_from(opts):
    traj = Trajectory()
    with open(opts.from_log, encoding="utf-8") as fh:
        for line in fh:
            try:
                traj.feed(json.loads(line))
            except ValueError:
                continue
    name = re.sub(r"^\d{8}T\d{6}Z-", "", os.path.splitext(os.path.basename(opts.from_log))[0])
    return finish(traj, name, opts.from_log, True)


def finish(traj, phase_name, log_path, trace):
    r = traj.result or {}
    print("\n=== result ===")
    print((r.get("result") or "(no final assistant text)").strip())
    if not traj.result or traj.tools_available is None:
        print("\nWARNING: init or result message missing from the stream; the run did not "
              "complete normally or the SDK message shapes changed.")
    print("\n" + traj.stats_line())
    if trace:
        md = traj.summary_md(phase_name, log_path)
        out = os.path.splitext(log_path)[0] + ".summary.md"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("\n=== trace ===\n" + md)
        print(f"summary written to {out}")
    return 0 if r.get("subtype") == "success" and not r.get("is_error") else 1


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", help="phase markdown file")
    ap.add_argument("--arg", action="append", default=[], help="key=value for a {{key}} placeholder")
    ap.add_argument("--max-turns", type=int)
    ap.add_argument("--max-budget-usd", type=float)
    ap.add_argument("--model")
    ap.add_argument("--trace", action="store_true", help="print and save the trajectory evidence")
    ap.add_argument("--dry-run", action="store_true", help="print options and prompts, run nothing")
    ap.add_argument("--from", dest="from_log", help="summarize an existing .jsonl instead of running")
    opts = ap.parse_args(argv)
    if opts.from_log:
        return summarize_from(opts)
    if not opts.phase:
        ap.error("--phase is required unless --from is given")
    return run_phase(opts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
