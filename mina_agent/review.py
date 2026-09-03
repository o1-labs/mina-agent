"""PR review pack: everything about a PR that can be computed, computed.

`mina-agent review --pr N` builds harness/state/reviews/pr-N/ with:
  pack.md    title/description/comments (gh), changed files grouped by dune
             unit, interface changes first, blast radius from the derived
             graph, a reading order (dependencies before dependents), a
             mermaid change map, flags, and test candidates
  diff.md    per-file structural diff (difftastic) or unified diff
  base/ head/  the changed files at the merge base and at the PR head
The model phase reads the pack and diff; a human reads them too. Locations
are vscode://file links so they open in the editor.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

BUILD_CONFIG = ("flake.nix", "flake.lock", "opam.export", "dune-project")
BUILD_CONFIG_SUFFIX = (".opam", "/dune", "dune", "rust-toolchain.toml", "Cargo.toml", "Cargo.lock")
SUBMODULES = ("src/lib/snarky", "src/lib/crypto/proof-systems",
              "src/lib/crypto/kimchi_bindings/stubs/kimchi-stubs-vendors")


@dataclass
class Changed:
    path: str
    status: str            # A M D R
    old_path: str = None
    added: int = 0
    deleted: int = 0
    unit: dict = None      # {kind, key, dir}


@dataclass
class Pack:
    number: int
    meta: dict
    merge_base: str
    head: str
    files: list = field(default_factory=list)
    out: Path = None


def _git(repo, *args, check=True):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _gh(repo, *args):
    r = subprocess.run(["gh", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")
    return r.stdout


def vscode_link(path, line=None):
    p = str(path)
    return f"vscode://file{p}" + (f":{line}" if line else "")


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------

def fetch(repo, number):
    meta = json.loads(_gh(repo, "pr", "view", str(number), "--json",
                          "number,title,author,baseRefName,headRefName,headRefOid,body,url,state,"
                          "comments,reviews,additions,deletions"))
    ref = f"refs/mina-agent/pr-{number}"
    _git(repo, "fetch", "-q", "origin", meta["baseRefName"], f"pull/{number}/head:{ref}")
    head = _git(repo, "rev-parse", ref).strip()
    base = _git(repo, "merge-base", f"origin/{meta['baseRefName']}", ref).strip()
    return meta, base, head


def changed_files(repo, base, head):
    out = []
    status = _git(repo, "diff", "--name-status", "-M", "-z", base, head).split("\0")
    i = 0
    while i < len(status) and status[i]:
        st = status[i]
        if st.startswith("R"):
            out.append(Changed(path=status[i + 2], status="R", old_path=status[i + 1])); i += 3
        else:
            out.append(Changed(path=status[i + 1], status=st[0])); i += 2
    nums = {}
    for line in _git(repo, "diff", "--numstat", "-M", base, head).splitlines():
        a, d, p = line.split("\t", 2)
        if " => " in p:  # rename: "old => new" or "dir/{a => b}/x"
            p = out[[c.old_path for c in out].index(p.split(" => ")[0])].path if p.split(" => ")[0] in [c.old_path for c in out] else p
        nums[p] = (int(a) if a.isdigit() else 0, int(d) if d.isdigit() else 0)
    for c in out:
        c.added, c.deleted = nums.get(c.path, (0, 0))
    return out


def materialize(repo, pack):
    for c in pack.files:
        for rev, sub in ((pack.merge_base, "base"), (pack.head, "head")):
            src_path = c.old_path if (rev == pack.merge_base and c.old_path) else c.path
            if (rev == pack.merge_base and c.status == "A") or (rev == pack.head and c.status == "D"):
                continue
            r = subprocess.run(["git", "show", f"{rev}:{src_path}"], cwd=repo, capture_output=True)
            if r.returncode == 0:
                dest = pack.out / sub / c.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(r.stdout)


def annotate_units(pack):
    from . import tools
    for c in pack.files:
        try:
            u = tools.library_of(c.path)
            c.unit = {"kind": u["kind"], "key": u["key"], "dir": u["dir"]}
        except Exception:
            c.unit = None


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def is_interface(c):
    return c.path.endswith(".mli") or c.path.endswith("/dune") or c.path == "dune" or c.path.endswith("dune-project")


def flags(pack):
    from . import tools
    rust = tools.MANIFEST_DATA["boundary"]["rust_paths"]
    out = []
    for c in pack.files:
        p = c.path
        if any(p == r or p.startswith(r.rstrip("/") + "/") for r in rust):
            out.append(f"{p}: Rust boundary (read-only for the harness)")
        if p in BUILD_CONFIG or p.endswith(BUILD_CONFIG_SUFFIX) or p.startswith("nix/"):
            out.append(f"{p}: build configuration")
        if p in SUBMODULES:
            out.append(f"{p}: submodule pointer moved")
    return out


def blast_radius(pack):
    from . import tools
    libs = sorted({c.unit["key"] for c in pack.files if c.unit and c.unit["kind"] == "lib"})
    out = {}
    for lib in libs:
        d = tools.dependents_of(lib)
        out[lib] = d
    return out


def reading_order(pack):
    """Changed libraries, dependencies before dependents (Kahn over the
    changed subgraph); then non-library units."""
    from . import tools
    g = tools.GRAPH.get()
    libs = sorted({c.unit["key"] for c in pack.files if c.unit and c.unit["kind"] == "lib"})
    remaining, order = set(libs), []
    while remaining:
        ready = sorted(l for l in remaining if not (set(g["libraries"][l]["deps"]) & remaining - {l}))
        if not ready:  # cycle, should not happen in a dune graph
            ready = sorted(remaining)
        order.extend(ready)
        remaining -= set(ready)
    others = sorted({c.unit["key"] for c in pack.files if c.unit and c.unit["kind"] != "lib"})
    return order, others


def test_candidates(pack):
    from . import tools
    seen, out = set(), []
    for c in pack.files:
        if not c.unit or c.status == "D":
            continue
        try:
            for cand in tools.tests_for(c.path)["candidates"][:3]:
                if cand["name"] not in seen:
                    seen.add(cand["name"])
                    out.append(cand)
        except Exception:
            continue
    order = {"fast": 0, "unmeasured": 1, "slow": 2, "integration": 3}
    return sorted(out, key=lambda x: order.get(x["cost"], 9))


def mermaid(pack, radius, max_dependents=8):
    changed = set(radius)
    lines = ["```mermaid", "graph LR"]
    for lib in radius:
        lines.append(f'  {lib}["{lib}"]:::changed')
    from . import tools
    g = tools.GRAPH.get()
    for lib in radius:
        for dep in g["libraries"][lib]["deps"]:
            if dep in changed:
                lines.append(f"  {dep} --> {lib}")
    for lib, d in radius.items():
        for dep in d["libraries"][:max_dependents]:
            if dep not in changed:
                lines.append(f'  {dep}["{dep}"]')
                lines.append(f"  {lib} --> {dep}")
        extra = len(d["libraries"]) - max_dependents
        if extra > 0:
            lines.append(f'  {lib}_more["+{extra} more dependents"]')
            lines.append(f"  {lib} --> {lib}_more")
    lines.append("  classDef changed fill:#f6d365,stroke:#333,color:#000")
    lines.append("```")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_diff(repo, pack):
    difft = shutil.which("difft")
    parts = [f"# PR #{pack.number} diff\n",
             f"base {pack.merge_base[:10]} .. head {pack.head[:10]}  "
             f"({'difftastic structural diff' if difft else 'unified diff, difftastic not installed'})\n"]
    for c in pack.files:
        parts.append(f"\n## {c.path}  ({c.status} +{c.added} -{c.deleted})\n")
        base_f, head_f = pack.out / "base" / c.path, pack.out / "head" / c.path
        if difft and base_f.exists() and head_f.exists():
            r = subprocess.run([difft, "--display", "inline", "--color", "never", "--width", "120",
                                str(base_f), str(head_f)], capture_output=True, text=True)
            parts.append("```\n" + r.stdout.strip() + "\n```\n")
        else:
            r = subprocess.run(["git", "diff", pack.merge_base, pack.head, "--", c.path],
                               cwd=repo, capture_output=True, text=True)
            parts.append("```diff\n" + r.stdout.strip() + "\n```\n")
    return "\n".join(parts)


def render_pack(repo, pack, link_root):
    m = pack.meta
    radius = blast_radius(pack)
    order, others = reading_order(pack)
    fl = flags(pack)
    tests = test_candidates(pack)
    by_unit = {}
    for c in pack.files:
        key = f"{c.unit['kind']}:{c.unit['key']}" if c.unit else "(not a dune unit)"
        by_unit.setdefault(key, []).append(c)

    def link(c, line=None):
        return vscode_link(Path(link_root) / c.path, line)

    L = [f"# PR #{pack.number}: {m['title']}", "",
         f"author {m['author']['login']} · {m['headRefName']} → {m['baseRefName']} · {m['state']} · "
         f"+{m['additions']} -{m['deletions']} · {len(pack.files)} file(s) · {m['url']}", "",
         "## Description", "", (m.get("body") or "(none)").strip(), ""]
    comments = [c for c in m.get("comments", [])] + [r for r in m.get("reviews", []) if r.get("body")]
    comments = [c for c in comments if not (c.get("author") or {}).get("login", "").endswith(("[bot]", "mergify"))]
    if comments:
        L += ["## Discussion so far", ""]
        for c in comments:
            who = (c.get("author") or {}).get("login", "?")
            L.append(f"- **{who}**: {(c.get('body') or '').strip()[:600]}")
        L.append("")
    L += ["## Changed files by dune unit", ""]
    for key in sorted(by_unit, key=lambda k: (not k.startswith("lib:"), k)):
        L.append(f"### {key}")
        for c in sorted(by_unit[key], key=lambda c: (not is_interface(c), c.path)):
            tag = " **interface**" if is_interface(c) else ""
            ren = f" (renamed from {c.old_path})" if c.old_path else ""
            L.append(f"- [{c.path}]({link(c)}) {c.status} +{c.added} -{c.deleted}{tag}{ren}")
        L.append("")
    if fl:
        L += ["## Flags", ""] + [f"- {f}" for f in fl] + [""]
    L += ["## Blast radius (direct dependents from the dune graph)", ""]
    for lib, d in radius.items():
        L.append(f"- **{lib}**: {len(d['libraries'])} libraries, {len(d['executables'])} executables, "
                 f"{len(d['tests'])} test units")
        if d["libraries"]:
            L.append("  - libraries: " + ", ".join(d["libraries"][:20]) + (" …" if len(d["libraries"]) > 20 else ""))
        if d["executables"]:
            L.append("  - executables: " + ", ".join(d["executables"][:10]))
    L += ["", mermaid(pack, radius), ""]
    L += ["## Reading order (dependencies before dependents)", ""]
    n = 0
    for lib in order:
        for c in sorted(by_unit.get(f"lib:{lib}", []), key=lambda c: (not is_interface(c), c.path)):
            n += 1
            L.append(f"{n}. [{c.path}]({link(c)}) in **{lib}**" + (" (interface)" if is_interface(c) else ""))
    for u in others:
        for c in by_unit.get(f"exe:{u}", []) + by_unit.get(f"test:{u}", []):
            n += 1
            L.append(f"{n}. [{c.path}]({link(c)}) in {u}")
    for c in by_unit.get("(not a dune unit)", []):
        n += 1
        L.append(f"{n}. [{c.path}]({link(c)})")
    L += ["", "## Test candidates for the changed code", ""]
    L += [f"- `{t['name']}` [{t['cost']}] — {t['reason']}" for t in tests] or ["- (none found)"]
    L += ["", f"Diff: [diff.md]({vscode_link(pack.out / 'diff.md')}) · base and head copies under `{pack.out}`", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
# checkout: put the PR's code in the working tree so LSP and links are live
# --------------------------------------------------------------------------

def _dirty(repo):
    """Tracked modifications or staged changes (untracked files are fine)."""
    return [l for l in _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines() if l.strip()]


def _state_file(repo):
    return paths.state_dir(repo) / "reviews" / "checkout.json"


def active_checkout(repo):
    f = _state_file(repo)
    return json.loads(f.read_text()) if f.exists() else None


def checkout(repo, number):
    """`gh pr checkout N` on a clean tree, remembering where we were."""
    if active_checkout(repo):
        a = active_checkout(repo)
        raise RuntimeError(f"PR #{a['number']} is already checked out (from {a['previous']}); "
                           "run `mina-agent review --done` first")
    if _dirty(repo):
        raise RuntimeError("working tree has uncommitted changes; commit or stash them before --checkout")
    previous = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _gh(repo, "pr", "checkout", str(number))
    _state_file(repo).parent.mkdir(parents=True, exist_ok=True)
    _state_file(repo).write_text(json.dumps({"number": number, "previous": previous}))
    return previous


def done(repo):
    """Return to the branch that was checked out before `review --checkout`."""
    a = active_checkout(repo)
    if not a:
        raise RuntimeError("no PR is checked out by mina-agent review")
    if _dirty(repo):
        raise RuntimeError("working tree has uncommitted changes on the PR branch; commit, stash, or discard them first")
    _git(repo, "checkout", "-q", a["previous"])
    _state_file(repo).unlink()
    return a


# --------------------------------------------------------------------------
# the session's first message
# --------------------------------------------------------------------------

RULES = """\
You are guiding a human through pull request #{number} in the Mina monorepo.
Below is a navigation pack computed from the dune dependency graph and a
structural diff of every changed file. Use them; do not rediscover what they
already state.

Your job is navigation and explanation, not verdicts: what changed, why (from
the description and the diff), what depends on it, what to look at and in
what order, who calls a changed interface. Cite locations as file:line and
prefer the vscode:// links from the pack so the reader can click through.
Use the LSP tool and the mina-harness tools (dependents_of, deps_of, type_at,
definition, library_of) for anything about callers, types, or definitions;
Grep is a fallback, not a source of truth.

{checkout_note}

This session cannot edit or run anything: Edit, Write, and Bash are removed.
When the reader asks you to record something (a comment for the author, a
note), print the text for them to paste, and keep a running summary they
can ask for at the end.

Start by giving a two-paragraph orientation: what this PR does and the
reading order you recommend, then wait for questions.
"""

CHECKOUT_LIVE = ("The PR is checked out in the working tree, so LSP, type_at, definition and "
                 "file reads all see the PR's code, and the pack's links open the real files.")
CHECKOUT_NOT = ("The PR is NOT checked out. Reads and LSP see the base branch; the changed files "
                "at the PR head are under {head_dir} and the pack's links point there.")


def first_message(pack, checked_out):
    pack_md = (pack.out / "pack.md").read_text()
    diff_md = (pack.out / "diff.md").read_text()
    limit = 80_000
    if len(diff_md) > limit:
        diff_md = diff_md[:limit] + f"\n\n[diff truncated at {limit} characters; full diff in {pack.out / 'diff.md'}]\n"
    note = CHECKOUT_LIVE if checked_out else CHECKOUT_NOT.format(head_dir=pack.out / "head")
    return "\n".join([RULES.format(number=pack.number, checkout_note=note),
                      "# Navigation pack", "", pack_md, "", "# Structural diff", "", diff_md])


# --------------------------------------------------------------------------
# entry
# --------------------------------------------------------------------------

def build(repo, number, checked_out=False):
    """Build the pack; returns (Pack, pack_path, diff_path)."""
    meta, base, head = fetch(repo, number)
    pack = Pack(number=number, meta=meta, merge_base=base, head=head)
    pack.out = paths.state_dir(repo) / "reviews" / f"pr-{number}"
    pack.out.mkdir(parents=True, exist_ok=True)
    pack.files = changed_files(repo, base, head)
    materialize(repo, pack)
    annotate_units(pack)
    link_root = Path(repo) if checked_out else pack.out / "head"
    (pack.out / "pack.md").write_text(render_pack(repo, pack, link_root))
    (pack.out / "diff.md").write_text(render_diff(repo, pack))
    (pack.out / "meta.json").write_text(json.dumps(
        {"number": number, "merge_base": base, "head": head, "base_ref": meta["baseRefName"],
         "head_ref": meta["headRefName"], "url": meta["url"],
         "files": [{"path": c.path, "status": c.status, "added": c.added, "deleted": c.deleted,
                    "unit": c.unit, "interface": is_interface(c)} for c in pack.files]}, indent=1))
    return pack, pack.out / "pack.md", pack.out / "diff.md"
