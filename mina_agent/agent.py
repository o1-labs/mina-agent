"""The one seam to Claude Code.

Everything that launches a model session goes through here:
  * headless phases (`mina-agent fix-build-error ...`) via the Claude Agent
    SDK's query(), with an in-process post-edit check hook and a serialized
    run log (Bash is removed from phases, and the deny rules ride along);
  * interactive sessions (`mina-agent discuss`) via the `claude` TUI.

Both get the same walls: the deny list from data/settings.template.json, the
harness MCP server and nothing else (strict), the facts appended to the
claude_code system prompt, and the activated switch as the environment.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys

from . import lsp, paths
from .trajectory import Trajectory, to_record

# --------------------------------------------------------------------------
# hooks (in-process for headless runs; `mina-agent hook ...` for interactive)
# --------------------------------------------------------------------------

LOG = {"fh": None, "traj": None}


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


async def _post_edit_cb(inp, tool_use_id, ctx):
    return record_hook("PostToolUse", inp.get("tool_name"), inp.get("tool_input"),
                       post_edit_check_output(inp.get("tool_input")))


# --------------------------------------------------------------------------
# shared configuration
# --------------------------------------------------------------------------

def deny_rules():
    with open(paths.SETTINGS_TEMPLATE) as fh:
        return json.load(fh)["permissions"]["deny"]


def mina_agent_bin():
    return shutil.which("mina-agent") or os.path.abspath(sys.argv[0])


def mcp_config():
    return {"mcpServers": {paths.MCP_SERVER_NAME: {
        "type": "stdio", "command": mina_agent_bin(), "args": ["serve"]}}}


def system_addition():
    from . import tools
    return "\n".join(tools.facts())


# --------------------------------------------------------------------------
# headless
# --------------------------------------------------------------------------

def build_options(phase, env, *, max_turns=None, max_budget_usd=None, model=None):
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
    disallowed = list(dict.fromkeys(phase["disallowed_tools"] + deny_rules()))
    return ClaudeAgentOptions(
        system_prompt={"type": "preset", "preset": "claude_code", "append": system_addition()},
        mcp_servers=mcp_config()["mcpServers"],
        strict_mcp_config=True,
        allowed_tools=phase["allowed_tools"],
        disallowed_tools=disallowed,
        permission_mode=phase["permission_mode"],
        max_turns=max_turns or phase["max_turns"],
        max_budget_usd=max_budget_usd or phase["max_budget_usd"],
        model=model,
        cwd=env.repo,
        env=env.activate(),
        setting_sources=["project"],
        plugins=([{"type": "local", "path": str(lsp.plugin_dir(env.repo))}]
                 if lsp.plugin_dir(env.repo) else []),
        include_hook_events=True,
        hooks={"PostToolUse": [HookMatcher(matcher="Edit|Write", hooks=[_post_edit_cb], timeout=600)]},
        stderr=lambda line: STDERR.append(line),
    )


STDERR = []


def options_summary(o):
    d = {k: getattr(o, k) for k in ("allowed_tools", "disallowed_tools", "permission_mode",
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

def interactive_argv(first_message, repo, extra_disallowed=()):
    """`claude` TUI with the harness server, facts, and walls. The prompt is
    the first user message. --disallowedTools is variadic, so it goes last."""
    argv = ["claude", first_message,
            "--append-system-prompt", system_addition(),
            "--mcp-config", json.dumps(mcp_config()), "--strict-mcp-config"]
    if lsp.plugin_dir(repo):
        argv += ["--plugin-dir", str(lsp.plugin_dir(repo))]
    argv += ["--disallowedTools"] + list(extra_disallowed) + deny_rules()
    return argv


def run_interactive(first_message, env, extra_disallowed=()):
    argv = interactive_argv(first_message, env.repo, extra_disallowed)
    return subprocess.run(argv, cwd=env.repo, env=env.activate()).returncode
