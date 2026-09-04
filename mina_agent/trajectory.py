"""Run logs and the trajectory evidence built from them.

A run log is one JSON object per line. Lines are either serialized Claude
Agent SDK messages (kind = the dataclass name) or HarnessHook records written
by the in-process hooks in agent.py. Trajectory.feed() consumes both, live and
from a saved file, so `mina-agent trace <log>` shows the same thing the run
printed.
"""
import dataclasses
import json


def to_record(msg):
    """Serialize an SDK message to a plain dict (the .jsonl line format)."""
    d = {"kind": type(msg).__name__}
    if dataclasses.is_dataclass(msg):
        for f in dataclasses.fields(msg):
            v = getattr(msg, f.name)
            if f.name == "content" and isinstance(v, list):
                v = [{"block": type(b).__name__,
                      **(dataclasses.asdict(b) if dataclasses.is_dataclass(b) else {"value": b})}
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


def blocks(rec):
    """The content blocks of a logged message. SDK message content is
    `str | list[ContentBlock]`; a plain string (the auto-compaction summary
    the CLI emits as a user message, for one) carries no blocks."""
    c = rec.get("content")
    return c if isinstance(c, list) else []


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
        elif k == "AssistantMessage":
            for b in blocks(rec):
                if b.get("block") == "ToolUseBlock":
                    c = {"id": b["id"], "name": b["name"], "input": b.get("input") or {},
                         "result": None, "is_error": False}
                    self.calls.append(c)
                    self.by_id[c["id"]] = c
        elif k == "UserMessage":
            for b in blocks(rec):
                if b.get("block") == "ToolResultBlock" and b.get("tool_use_id") in self.by_id:
                    c = self.by_id[b["tool_use_id"]]
                    c["result"] = b.get("content")
                    c["is_error"] = bool(b.get("is_error"))
        elif k == "HarnessHook":
            self.hooks.append({"name": f"{rec['event']}:{rec['tool']}",
                               "input": rec.get("input"), "output": rec.get("output")})
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
        out = h.get("output") or {}
        hso = out.get("hookSpecificOutput") or {}
        ctx = hso.get("additionalContext") or ""
        try:
            hc = (json.loads(ctx).get("harness_check") or {}) if ctx.startswith("{") else {}
        except ValueError:
            hc = {}
        if hc:
            s = f"check {hc.get('alias')} ok={hc.get('ok')} errors={len(hc.get('errors', []))}"
            if hc.get("errors"):
                e0 = hc["errors"][0]
                s += f" first={e0['file']}:{e0['line']} {short(e0['message'], 60)}"
            return s
        return short(hso.get("permissionDecisionReason") or ctx or "", 100) or "no action"

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
        L += [f"- {h['name']} {short(h.get('input') or '', 60)} -> {self.hook_detail(h)}"
              for h in self.hooks] or ["(none recorded)"]
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


def load(log_path):
    traj = Trajectory()
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                traj.feed(json.loads(line))
            except ValueError:
                continue
    return traj
