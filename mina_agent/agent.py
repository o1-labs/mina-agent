"""The one seam to Claude Code.

Everything that launches a model session goes through here:
  * headless phases (`mina-agent fix-build-error ...`) via the Claude Agent
    SDK's query(), with an in-process post-edit check hook and a serialized
    run log (Bash is removed from phases, and the deny rules ride along);
  * interactive sessions (`mina-agent discuss`) via the `claude` TUI.

Both get the same walls from data/settings.template.json (deny rules and
hooks), the harness MCP server and nothing else (strict), the facts appended
to the claude_code system prompt, and the activated switch as the
environment. Nothing is installed into the repo's .claude/, the user's MCP
registry, or the skills directory: the harness applies only when invoked.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from typing import Any, cast

from . import lsp, paths
from .model import Phase
from .trajectory import Trajectory, to_record

# --------------------------------------------------------------------------
# hooks (in-process for headless runs; `mina-agent hook ...` for interactive)
# --------------------------------------------------------------------------

LOG: dict[str, Any] = {"fh": None, "traj": None}


def record_hook(event, tool, inp, output):
    """Write a HarnessHook line into the run log from inside a callback, so the
    trace has first-hand evidence of every hook firing (the SDK does not
    surface callback events in the message stream)."""
    rec = {"kind": "HarnessHook", "event": event, "tool": tool,
           "input": {k: v for k, v in (inp or {}).items() if k in ("file_path", "command")},
           "output": output}
    if LOG["fh"]:
        LOG["fh"].write(json.dumps(rec, default=str) + "\n")
        LOG["fh"].flush()
    if LOG["traj"]:
        LOG["traj"].feed(json.loads(json.dumps(rec, default=str)))
    return output


def post_edit_check_output(tool_input):
    """PostToolUse Edit|Write: type-check an edited .ml/.mli. Returns the hook
    JSON (empty dict when not applicable)."""
    path = (tool_input or {}).get("file_path") or ""
    if not path.endswith((".ml", ".mli")):
        return {}
    from . import tools
    try:
        r = tools.check(path)
    except Exception as ex:
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                       "additionalContext": f"harness check skipped for {path}: {ex}"}}
    ctx = {"harness_check": {"path": r["path"], "alias": r["alias"], "ok": r["ok"],
                             "elapsed_s": r["elapsed_s"], "errors": r["errors"],
                             "warnings": r["warnings"][:10]}}
    if not r["ok"] and not r["errors"]:
        ctx["harness_check"]["raw_tail"] = r["raw_tail"]
    status = "ok" if r["ok"] else f"{len(r['errors'])} error(s)"
    first = f" ({r['errors'][0]['file']}:{r['errors'][0]['line']})" if r["errors"] else ""
    return {"systemMessage": f"harness check {r['alias']}: {status}{first} in {r['elapsed_s']}s",
            "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                   "additionalContext": json.dumps(ctx)}}


async def _pre_bash_cb(inp, tool_use_id, ctx):
    """Headless counterpart of `mina-agent hook pre-bash`. The SDK calls it
    once per matching HookMatcher, so no per-call dedupe is needed here."""
    from .commands.hook import DENIAL_CONTEXT
    return record_hook("PreToolUse", "Bash", inp.get("tool_input"),
                       {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                               "additionalContext": DENIAL_CONTEXT}})


async def _post_edit_cb(inp, tool_use_id, ctx):
    return record_hook("PostToolUse", inp.get("tool_name"), inp.get("tool_input"),
                       post_edit_check_output(inp.get("tool_input")))


# --------------------------------------------------------------------------
# shared configuration
# --------------------------------------------------------------------------

def _template():
    with open(paths.SETTINGS_TEMPLATE) as fh:
        t = json.load(fh)
    t.pop("_comment", None)
    return t


def deny_rules():
    return _template()["permissions"]["deny"]


BASH_CONTEXT_HOOK = {"type": "command", "command": "mina-agent hook pre-bash", "timeout": 10}


def bash_deny_rules():
    return [r for r in deny_rules() if r.startswith("Bash(")]


def _hooks():
    """The template's hooks plus one PreToolUse Bash entry per Bash deny rule,
    gated by that rule through the hook `if` field. The deny list is the
    single source of truth: the entry only attaches context, Claude Code's
    matcher does the enforcing."""
    hooks = _template()["hooks"]
    bash = {"matcher": "Bash", "hooks": [{**BASH_CONTEXT_HOOK, "if": r} for r in bash_deny_rules()]}
    return {**hooks, "PreToolUse": [*hooks.get("PreToolUse", []), bash]}


def mina_agent_bin():
    return shutil.which("mina-agent") or os.path.abspath(sys.argv[0])


def mcp_config():
    return {"mcpServers": {paths.MCP_SERVER_NAME: {
        "type": "stdio", "command": mina_agent_bin(), "args": ["serve"]}}}


def headless_settings():
    """The template without its hooks: headless runs get hooks in-process via
    sdk_hooks(), so the file passed as ClaudeAgentOptions.settings carries
    only the permission rules and flags (includeCoAuthoredBy)."""
    return {k: v for k, v in _template().items() if k != "hooks"}


def develop_config():
    """manifest.toml [develop]: the shell allowlist and what runs unprompted."""
    import tomllib
    with open(paths.MANIFEST, "rb") as fh:
        return tomllib.load(fh)["develop"]


def session_settings(develop=False):
    """The settings an interactive harness session runs with, passed via
    `claude --settings` so nothing is written into the repo's .claude/.
    Hook commands point at this binary. develop=True adds the shell
    allowlist hook, the unprompted commands, and denies the non-developer
    tools."""
    t = _template()
    t["hooks"] = _hooks()
    if develop:
        cfg = develop_config()
        t["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [{"type": "command", "command": "mina-agent hook bash-allowlist",
                                                                    "timeout": 10}]}, *t["hooks"]["PreToolUse"]]
        t["permissions"]["allow"] = [*t["permissions"].get("allow", []), *cfg["auto_allow"]]
        t["permissions"]["deny"] = [*cfg["deny_tools"], *t["permissions"]["deny"]]
    binp = mina_agent_bin()
    for matchers in t["hooks"].values():
        for m in matchers:
            for h in m["hooks"]:
                h["command"] = h["command"].replace("mina-agent", f'"{binp}"', 1)
    return t


# hook command suffix -> in-process callback, for the SDK path
def _callbacks():
    return {"hook post-edit": _post_edit_cb, "hook pre-bash": _pre_bash_cb}


def sdk_hooks():
    """HookMatcher list for headless runs, generated from the same template.
    SessionStart is skipped: headless runs get the facts via the system
    prompt. Entries whose command has no in-process equivalent are skipped."""
    from claude_agent_sdk import HookMatcher
    out = {}
    for event, matchers in _hooks().items():
        if event == "SessionStart":
            continue
        for m in matchers:
            fns, timeout = [], None
            for h in m["hooks"]:
                for suffix, fn in _callbacks().items():
                    if h["command"].endswith(suffix) or f"{suffix} " in h["command"]:
                        if fn not in fns:
                            fns.append(fn)
                        timeout = max(timeout or 0, h.get("timeout", 60))
            if fns:
                out.setdefault(event, []).append(HookMatcher(matcher=m.get("matcher"), hooks=fns, timeout=timeout))
    return out


def system_addition():
    from . import tools
    return "\n".join(tools.facts())


# --------------------------------------------------------------------------
# headless
# --------------------------------------------------------------------------

def build_options(phase: Phase, env, *, max_turns=None, max_budget_usd=None, model=None):
    from claude_agent_sdk import ClaudeAgentOptions
    disallowed = list(dict.fromkeys([*phase.disallowed_tools, *deny_rules()]))
    # The base built-in set is exactly what the phase allows. With the full
    # Claude Code inventory (45 tools) the CLI defers the MCP tools behind
    # ToolSearch and the model spends its first turn loading them.
    # "Bash(git *)" allows a command pattern; the tool it names is Bash.
    builtin = list(dict.fromkeys(t.split("(")[0] for t in phase.allowed_tools if not t.startswith("mcp__")))
    settings_file = paths.state_dir() / "headless-settings.json"
    settings_file.write_text(json.dumps(headless_settings(), indent=1))
    return ClaudeAgentOptions(
        tools=builtin,
        settings=str(settings_file),
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_addition()},
        mcp_servers=cast(Any, mcp_config()["mcpServers"]),
        strict_mcp_config=True,
        allowed_tools=list(phase.allowed_tools),
        disallowed_tools=disallowed,
        permission_mode=cast(Any, phase.permission_mode),
        max_turns=max_turns or phase.max_turns,
        max_budget_usd=max_budget_usd or phase.max_budget_usd,
        model=model,
        cwd=env.repo,
        env=env.session_env(),
        setting_sources=["project"],
        plugins=([{"type": "local", "path": str(lsp.plugin_dir(env.repo))}]
                 if lsp.plugin_dir(env.repo) else []),
        include_hook_events=True,
        hooks=sdk_hooks(),
        stderr=lambda line: STDERR.append(line),
    )


STDERR = []


def options_summary(o):
    d = {k: getattr(o, k) for k in ("tools", "allowed_tools", "disallowed_tools", "permission_mode",
                                    "max_turns", "max_budget_usd", "model", "cwd",
                                    "setting_sources", "strict_mcp_config")}
    d["mcp_servers"] = {k: v["command"] + " " + " ".join(v["args"]) for k, v in o.mcp_servers.items()}
    d["hooks"] = {k: [m.matcher for m in v] for k, v in (o.hooks or {}).items()}
    d["system_prompt_append_chars"] = len(o.system_prompt.get("append", ""))
    d["env_overrides"] = sorted(k for k, v in o.env.items() if os.environ.get(k) != v)
    return d


async def _run(prompt, options, log_path, traj, on_call):
    from claude_agent_sdk import query
    seen = 0
    with open(log_path, "w", encoding="utf-8") as log:
        LOG["fh"], LOG["traj"] = log, traj
        try:
            async for msg in query(prompt=prompt, options=options):
                rec = to_record(msg)
                log.write(json.dumps(rec) + "\n")
                log.flush()
                traj.feed(rec)
                for c in traj.calls[seen:]:
                    on_call(traj, c)
                seen = len(traj.calls)
        finally:
            LOG["fh"], LOG["traj"] = None, None


def run_headless(prompt, options, log_path, on_call=lambda traj, c: None):
    """Run one headless session; returns the Trajectory."""
    traj = Trajectory()
    asyncio.run(_run(prompt, options, log_path, traj, on_call))
    return traj


# --------------------------------------------------------------------------
# interactive
# --------------------------------------------------------------------------

def interactive_argv(first_message, repo, develop=False, resume=None):
    """`claude` TUI with the harness server, facts, and walls. Everything the
    session needs travels on the command line (--settings, --mcp-config,
    --plugin-dir); nothing is read from or written to the repo's .claude/.
    develop=True: edits accepted without asking, shell allowlist.
    resume: a Claude session id to continue instead of a fresh session with
    first_message."""
    argv = ["claude", *(["--resume", resume] if resume else [first_message]),
            "--append-system-prompt", system_addition(),
            "--settings", json.dumps(session_settings(develop)),
            "--mcp-config", json.dumps(mcp_config()), "--strict-mcp-config"]
    if develop:
        argv += ["--permission-mode", "acceptEdits"]
    if lsp.plugin_dir(repo):
        argv += ["--plugin-dir", str(lsp.plugin_dir(repo))]
    from . import plugins
    for d in plugins.dirs(repo):          # declared external plugins, synced by init
        argv += ["--plugin-dir", str(d)]
    return argv


def run_interactive(first_message, env, mode, develop=False, resume=None):
    """Launch the TUI; `mode` (discuss, develop, profile) is passed to the
    hooks so the session is recorded for --continue."""
    from . import sessions
    argv = interactive_argv(first_message, env.repo, develop, resume)
    return subprocess.run(argv, cwd=env.repo, env={**env.session_env(), sessions.MODE_VAR: mode}).returncode


def resume_id(mode) -> str:
    """The last session of `mode`, or a typer exit with the reason."""
    from . import sessions
    import typer
    sid = sessions.last(mode)
    if not sid:
        typer.echo(f"no previous {mode} session recorded (sessions are logged from their SessionStart hook)", err=True)
        raise typer.Exit(2)
    return sid
