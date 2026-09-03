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
import html
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


def _unit_deps(key, kind):
    """Direct dependency library names of a changed unit, from derived.json."""
    from . import tools
    g = tools.GRAPH.get()
    table = {"lib": "libraries", "exe": "executables", "test": "tests"}[kind]
    rec = g[table].get(key) or {}
    return rec.get("deps", [])


def _reaches(src_key, src_kind, targets):
    """The libraries in `targets` reachable from a unit via the dependency
    graph, following library deps transitively through unchanged libs too."""
    from . import tools
    g = tools.GRAPH.get()
    seen, out, stack = set(), set(), list(_unit_deps(src_key, src_kind))
    while stack:
        lib = stack.pop()
        if lib in seen or lib not in g["libraries"]:
            continue
        seen.add(lib)
        if lib in targets:
            out.add(lib)
        stack.extend(g["libraries"][lib]["deps"])
    return out


def changed_units(pack):
    """The distinct changed dune units, as (kind, key). Files with no unit
    (config, changelog) are returned separately."""
    units, non = [], []
    seen = set()
    for c in pack.files:
        if c.unit:
            k = (c.unit["kind"], c.unit["key"])
            if k not in seen:
                seen.add(k)
                units.append(k)
        elif c.path not in non:
            non.append(c.path)
    return units, non


def topo_units(pack):
    """Topological order of the changed units, dependencies before dependents,
    transitive-aware (edges count even when the path runs through unchanged
    libraries). Returns (order, edges, roots):
      order  list of (kind, key), foundation first
      edges  transitive-reduced {dependent_key: [dependency_key, ...]} among
             changed *library* units (an arrow points dependency -> dependent)
      roots  keys with no changed-library dependency (forest roots)
    """
    units, _ = changed_units(pack)
    lib_keys = {key for kind, key in units if kind == "lib"}
    # dep[u] = changed libs that u depends on (u is a lib/exe/test)
    dep = {}
    for kind, key in units:
        targets = lib_keys - {key}
        dep[(kind, key)] = _reaches(key, kind, targets)
    # topo sort (Kahn): a unit is ready when all its changed-lib deps are placed
    remaining = list(units)
    placed, order = set(), []
    while remaining:
        ready = [u for u in remaining if dep[u] <= placed]
        if not ready:                       # cycle guard (shouldn't happen)
            ready = remaining[:]
        ready.sort(key=lambda u: (u[0] != "lib", u[1]))
        order.extend(ready)
        placed |= {key for _, key in ready}
        remaining = [u for u in remaining if u not in ready]
    # transitive reduction for the map: keep dep A->B only if no changed lib C
    # with A->C and C->B (edges are among library units)
    libdep = {key: (dep[("lib", key)] & lib_keys) for key in lib_keys}
    reduced = {}
    for a, bs in libdep.items():
        keep = {b for b in bs if not any(b in libdep.get(c, set()) for c in bs if c != b)}
        reduced[a] = sorted(keep)
    roots = sorted(k for k in lib_keys if not libdep[k])
    return order, reduced, roots


def test_candidates(pack):
    from . import tools
    # One tests_for per changed unit, not per file: files in the same library
    # share candidates, and tests_for walks the dependents, which is the
    # expensive part. One representative path per unit.
    rep = {}
    for c in pack.files:
        if c.unit and c.status != "D":
            rep.setdefault((c.unit["kind"], c.unit["key"]), c.path)
    seen, out = set(), []
    for path in rep.values():
        try:
            for cand in tools.tests_for(path)["candidates"][:3]:
                if cand["name"] not in seen:
                    seen.add(cand["name"])
                    out.append(cand)
        except Exception:
            continue
    order = {"fast": 0, "unmeasured": 1, "slow": 2, "integration": 3}
    return sorted(out, key=lambda x: order.get(x["cost"], 9))


def _diff_slug(path):
    return path.replace("/", "__")


def per_file_diffs(repo, pack):
    """Write one difft per changed file to diffs/<slug>.md; return {path: file}."""
    difft = shutil.which("difft")
    outdir = pack.out / "diffs"
    outdir.mkdir(parents=True, exist_ok=True)
    written = {}
    for c in pack.files:
        base_f, head_f = pack.out / "base" / c.path, pack.out / "head" / c.path
        body = None
        if difft and base_f.exists() and head_f.exists():
            r = subprocess.run([difft, "--display", "inline", "--color", "never", "--width", "120",
                                str(base_f), str(head_f)], capture_output=True, text=True)
            body = "```\n" + r.stdout.strip() + "\n```"
        else:
            r = subprocess.run(["git", "diff", pack.merge_base, pack.head, "--", c.path],
                               cwd=repo, capture_output=True, text=True)
            body = "```diff\n" + r.stdout.strip() + "\n```"
        f = outdir / (_diff_slug(c.path) + ".md")
        f.write_text(f"# {c.path}  ({c.status} +{c.added} -{c.deleted})\n\n{body}\n")
        written[c.path] = f
    return written


def order_files(pack):
    """Changed files in dependency order: for each unit in topological order,
    its files (interface first); then files with no unit. Returns a list of
    (rank, file_change, unit_key_or_None)."""
    order, _, _ = topo_units(pack)
    _, non = changed_units(pack)
    by_unit = {}
    for c in pack.files:
        key = (c.unit["kind"], c.unit["key"]) if c.unit else None
        by_unit.setdefault(key, []).append(c)
    out, rank = [], 0
    for u in order:
        for c in sorted(by_unit.get(u, []), key=lambda c: (not is_interface(c), c.path)):
            rank += 1
            out.append((rank, c, u[1]))
    for c in by_unit.get(None, []):
        rank += 1
        out.append((rank, c, None))
    return out


def dot_forest(pack, url_for):
    """Graphviz DOT: changed files as nodes, grouped in per-unit clusters,
    dependency edges between units (foundation at top). Each node's URL is
    url_for(path). Foundation-first reading order runs top to bottom."""
    order, reduced, _ = topo_units(pack)
    by_unit = {}
    for c in pack.files:
        by_unit.setdefault((c.unit["kind"], c.unit["key"]) if c.unit else None, []).append(c)
    L = ['digraph changes {', '  rankdir=TB;', '  compound=true;', '  node [shape=box, style="rounded,filled", '
         'fillcolor="#eef3fb", fontname="monospace", fontsize=10];', '  edge [color="#888888", arrowsize=0.7];']
    rep = {}   # unit -> a representative node id, for cluster-to-cluster edges
    nid = 0
    for kind, key in order:
        L.append(f'  subgraph "cluster_{kind}_{key}" {{ label="{key}"; style="rounded"; color="#c9d4e6";')
        for c in sorted(by_unit.get((kind, key), []), key=lambda c: (not is_interface(c), c.path)):
            nid += 1
            node = f"n{nid}"
            rep.setdefault((kind, key), node)
            label = os.path.basename(c.path) + ("  (interface)" if is_interface(c) else "")
            fill = "#f6d365" if is_interface(c) else "#eef3fb"
            L.append(f'    {node} [label="{label}", URL="{url_for(c.path)}", fillcolor="{fill}"];')
        L.append("  }")
    # edges dependency -> dependent (foundation points up-tree to dependents)
    for dependent, deps in reduced.items():
        for d in deps:
            if ("lib", d) in rep and ("lib", dependent) in rep:
                L.append(f'  {rep[("lib", d)]} -> {rep[("lib", dependent)]} '
                         f'[ltail="cluster_lib_{d}", lhead="cluster_lib_{dependent}"];')
    for c in by_unit.get(None, []):          # non-unit files (config, changelog) as loose roots
        nid += 1
        L.append(f'  n{nid} [label="{os.path.basename(c.path)}", URL="{url_for(c.path)}", fillcolor="#f0f0f0"];')
    L.append("}")
    return "\n".join(L)


def render_map(pack, diff_files, link_root):
    """Write map.svg (graphviz) whose nodes open each file's difft; return the
    markdown to embed. Falls back to a note when graphviz is absent."""
    if shutil.which("dot"):
        svg = pack.out / "map.svg"
        r = subprocess.run(["dot", "-Tsvg", "-o", str(svg)],
                           input=dot_forest(pack, lambda p: vscode_link(diff_files[p])),
                           capture_output=True, text=True)
        if r.returncode == 0 and svg.exists():
            return (f"![change map]({svg.name})\n\n(foundation at top, dependents below; yellow = interface. "
                    "The image is static; use the interactive HTML page below for clickable nodes and diffs.)")
    return "(install graphviz for a rendered map: brew install graphviz)"


# --------------------------------------------------------------------------
# review.html: clickable change map + semantic (difftastic) diffs in one page
# --------------------------------------------------------------------------

def _svg_inline(pack):
    """Graphviz SVG with node links as in-page anchors (#file-slug)."""
    if not shutil.which("dot"):
        return None
    dot = dot_forest(pack, lambda p: "#" + _diff_slug(p))
    r = subprocess.run(["dot", "-Tsvg"], input=dot, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    svg = r.stdout[r.stdout.index("<svg"):]           # drop xml/doctype preamble
    return svg.replace("%23", "#")                    # graphviz percent-encodes '#'


def build_html(repo, pack, diff_files):
    """Self-contained review.html: PR header, topological table of contents,
    inline clickable change map, and every file's difftastic diff rendered in
    colour. No server, opens in any browser."""
    from . import ansi
    m = pack.meta
    difft = shutil.which("difft")
    order = order_files(pack)

    toc = []
    for rank, c, unit in order:
        slug = _diff_slug(c.path)
        where = f" · {unit}" if unit else ""
        tag = " (interface)" if is_interface(c) else ""
        toc.append(f'<li><a href="#{slug}">{rank}. {html.escape(c.path)}</a>'
                   f'<span class="dim">{html.escape(tag + where)}</span></li>')

    sections = []
    for rank, c, unit in order:
        slug = _diff_slug(c.path)
        base_f, head_f = pack.out / "base" / c.path, pack.out / "head" / c.path
        if difft and base_f.exists() and head_f.exists():
            raw = subprocess.run([difft, "--color=always", "--display", "side-by-side", "--width", "150",
                                  str(base_f), str(head_f)], capture_output=True, text=True).stdout
            body = ansi.to_html(raw)
        else:
            raw = subprocess.run(["git", "diff", pack.merge_base, pack.head, "--", c.path],
                                 cwd=repo, capture_output=True, text=True).stdout
            body = html.escape(raw)
        head_link = vscode_link(Path(pack.out) / "head" / c.path)
        sections.append(
            f'<section id="{slug}"><h2>{rank}. {html.escape(c.path)} '
            f'<span class="meta">{c.status} +{c.added} -{c.deleted}{" · interface" if is_interface(c) else ""}'
            f'{(" · " + html.escape(unit)) if unit else ""}</span> '
            f'<a class="open" href="{head_link}">open file</a></h2>'
            f'<pre>{body}</pre></section>')

    svg = _svg_inline(pack)
    map_block = (f'<div class="map">{svg}</div>' if svg
                 else '<p class="dim">install graphviz for the change map: brew install graphviz</p>')
    return HTML_TEMPLATE.format(
        title=html.escape(f"PR #{pack.number}: {m['title']}"),
        subtitle=html.escape(f"{m['author']['login']} · {m['headRefName']} -> {m['baseRefName']} · "
                             f"+{m['additions']} -{m['deletions']} · {len(pack.files)} file(s)"),
        url=m["url"], toc="\n".join(toc), map=map_block, sections="\n".join(sections))


HTML_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><style>
 :root {{ --bg:#0f1115; --panel:#161a22; --line:#252b37; --fg:#d6dae2; --dim:#8a92a3; --link:#5aa9e6; }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; background:var(--bg); color:var(--fg); font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace }}
 a {{ color:var(--link); text-decoration:none }} a:hover {{ text-decoration:underline }}
 header {{ padding:14px 18px; border-bottom:1px solid var(--line) }}
 header h1 {{ margin:0 0 4px; font-size:15px }} .dim {{ color:var(--dim) }}
 .cols {{ display:grid; grid-template-columns: 300px 1fr; gap:0; align-items:start }}
 nav {{ position:sticky; top:0; align-self:start; max-height:100vh; overflow:auto; padding:12px 14px; border-right:1px solid var(--line) }}
 nav h2 {{ font-size:11px; text-transform:uppercase; letter-spacing:.08em; color:var(--dim); margin:10px 0 6px }}
 nav ol {{ list-style:none; margin:0; padding:0 }} nav li {{ margin:3px 0 }}
 .map {{ background:var(--panel); border-radius:6px; padding:10px; margin:8px 0; overflow:auto }}
 .map svg {{ max-width:100% }}
 main {{ padding:12px 18px; overflow:auto }}
 section {{ margin:0 0 22px; border:1px solid var(--line); border-radius:6px }}
 section h2 {{ font-size:13px; margin:0; padding:8px 12px; background:var(--panel); border-bottom:1px solid var(--line);
   position:sticky; top:0 }}
 section .meta {{ color:var(--dim); font-weight:normal }} section .open {{ float:right; font-weight:normal }}
 pre {{ margin:0; padding:10px 12px; overflow:auto; white-space:pre; font-size:12px; line-height:1.45 }}
</style></head><body>
<header><h1>{title}</h1><div class="dim">{subtitle} · <a href="{url}">{url}</a></div></header>
<div class="cols">
 <nav>
   <h2>change map</h2>{map}
   <h2>reading order</h2><ol>{toc}</ol>
   <p class="dim">Sorted so a file comes after everything it depends on. Click a map node or a list item.</p>
 </nav>
 <main>{sections}</main>
</div></body></html>"""


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


def render_pack(repo, pack, link_root, diff_files):
    m = pack.meta
    fl = flags(pack)
    tests = test_candidates(pack)
    by_unit = {}
    for c in pack.files:
        key = f"{c.unit['kind']}:{c.unit['key']}" if c.unit else "(not a dune unit)"
        by_unit.setdefault(key, []).append(c)

    def link(c, line=None):
        return vscode_link(Path(link_root) / c.path, line)

    def difflink(c):
        return vscode_link(diff_files[c.path])

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
    L += ["## Reading order", "",
          "The changed files, sorted so a file comes after everything it depends on "
          "(dependencies through unchanged code count too). Read top to bottom. "
          "Each links to its own diff; the source link is in parentheses.", ""]
    for rank, c, unit in order_files(pack):
        where = f" — {unit}" if unit else ""
        tag = " (interface)" if is_interface(c) else ""
        L.append(f"{rank}. [{c.path}]({difflink(c)}){tag}{where}  ([source]({link(c)}))")
    L += ["", "## Change map", "", render_map(pack, diff_files, link_root),
          "", f"**Best view:** open [review.html]({vscode_link(pack.out / 'review.html')}) in a browser "
          "(`mina-agent review --open`) for a clickable map and the semantic diffs in colour.", ""]
    L += ["## Test candidates for the changed code", ""]
    L += [f"- `{t['name']}` [{t['cost']}] — {t['reason']}" for t in tests] or ["- (none found)"]
    L += ["", f"Full diff: [diff.md]({vscode_link(pack.out / 'diff.md')}) · per-file diffs under "
          f"`{pack.out / 'diffs'}` · base and head copies under `{pack.out}`", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
# checkout: put the PR's code in the working tree so LSP and links are live
# --------------------------------------------------------------------------

def _dirty(repo):
    """Tracked modifications or staged changes. Untracked files are fine, and
    so are submodule pointer differences: checking out a PR branch moves the
    recorded submodule commits without updating the submodules."""
    out = _git(repo, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all")
    return [l for l in out.splitlines() if l.strip()]


def installed_from_repo(repo):
    """True when the running package lives inside the checkout (an editable
    install). Checking out a branch without harness/ would then delete the
    tool from under itself."""
    return str(paths.PKG).startswith(str(Path(repo).resolve()) + os.sep)


def _state_file(repo):
    # Outside reviews/ so clearing generated review assets never loses the
    # record of a checked-out PR.
    return paths.state_dir(repo) / "review-checkout.json"


def active_checkout(repo):
    f = _state_file(repo)
    return json.loads(f.read_text()) if f.exists() else None


def _sync_submodules(repo):
    """Match the submodule working trees to the current branch. A PR based on
    a different base (e.g. develop vs compatible) records different commits
    for proof-systems and the kimchi vendors; git does not update submodule
    working trees on `checkout`, so without this the compiled Rust stubs and
    the OCaml bindings disagree (gate_type mismatch) and nothing builds."""
    _git(repo, "submodule", "update", "--init", "--recursive")


def checkout(repo, number):
    """`gh pr checkout N` on a clean tree, then sync submodules to the branch."""
    if active_checkout(repo):
        a = active_checkout(repo)
        raise RuntimeError(f"PR #{a['number']} is already checked out (from {a['previous']}); "
                           "run `mina-agent review --done` first")
    if _dirty(repo):
        raise RuntimeError("working tree has uncommitted changes; commit or stash them before --checkout")
    if installed_from_repo(repo):
        raise RuntimeError("mina-agent is installed editable from this checkout; a branch without harness/ "
                           "would remove it. Reinstall non-editable: uv tool install ./harness --reinstall")
    previous = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _gh(repo, "pr", "checkout", str(number))
    _sync_submodules(repo)
    _state_file(repo).parent.mkdir(parents=True, exist_ok=True)
    _state_file(repo).write_text(json.dumps({"number": number, "previous": previous}))
    return previous


def done(repo):
    """Return to the branch checked out before --checkout, and sync submodules
    back to it. Regenerated JS build artifacts (committed .node/.d.ts the
    build overwrites) are discarded, since they are not the reviewer's work."""
    a = active_checkout(repo)
    if not a:
        raise RuntimeError("no PR is checked out by mina-agent review")
    _git(repo, "checkout", "--", "src/lib/crypto/kimchi_bindings/js/native/artifacts", check=False)
    if _dirty(repo):
        raise RuntimeError("working tree has uncommitted changes on the PR branch; commit, stash, or discard them first")
    _git(repo, "checkout", "-q", a["previous"])
    _sync_submodules(repo)
    _state_file(repo).unlink()
    return a


# --------------------------------------------------------------------------
# the session's first message
# --------------------------------------------------------------------------

RULES = """\
You are helping a reviewer understand pull request #{number} in the Mina
monorepo. The reviewer did not write this code and is reading it for the
first time. Below is a navigation pack computed from the dune dependency
graph and a structural diff of every changed file. Use them; do not
rediscover what they already state.

How to talk to the reader:
- Plain language. Short sentences. One idea per sentence.
- Say the thing, then stop. No preamble, no recap, no praise of the PR.
- Answer only what was asked. Offer the single most useful next step, in one
  line, and wait.
- Small pieces. Never more than one function or one hunk per reply unless
  asked for more. When something is big, say "this has three parts" and
  give the first.
- Every identifier you mention is a link. A function, type, constructor,
  module, record field, flag, or variant name never appears bare: the first
  time it appears in a reply it is written as [name](vscode://file/...:line),
  pointing at its definition, or at the changed line where it appears in
  this PR. This includes the opening summary. If you cannot find where it
  is defined, do not name it; describe it and say you could not locate it.
- Every other claim about code points at a line the same way.
- Name things the way the code names them. If a term needs a definition,
  give it in half a sentence, once.
- If you do not know, say so and say what you would look at.

What you are for: what changed, why (from the description and the diff),
what depends on it, what to read next, who calls a changed interface. Not
verdicts: do not judge the PR unless asked, and then say what you looked at.
For callers, types, and definitions use the LSP tool and the mina-harness
tools (dependents_of, deps_of, type_at, definition, library_of); Grep is a
fallback, and say when you used it.

{checkout_note}

This session cannot edit or run anything: Edit, Write, and Bash are removed.
When the reader asks you to record something (a comment for the author, a
note), print the text for them to paste.

Your first reply, in this order:
1. One line, first thing, leading with the review page: "Open the review page: {html_link} (clickable change map + colored diffs). Also: {pack_link}." Use those exact links.
2. What this PR changes, in one sentence.
3. Why, in one sentence.
4. Where to start reading, one link.
5. A question offering the next step.
Every identifier in lines 2 to 5 is a link too. Keep it to five lines after
the review-page line. Then wait.
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
    html_link = f"[review.html]({vscode_link(pack.out / 'review.html')})"
    pack_link = f"[pack.md]({vscode_link(pack.out / 'pack.md')})"
    rules = RULES.format(number=pack.number, checkout_note=note,
                         html_link=html_link, pack_link=pack_link)
    return "\n".join([rules, "# Navigation pack", "", pack_md, "", "# Structural diff", "", diff_md])


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
    diff_files = per_file_diffs(repo, pack)
    (pack.out / "pack.md").write_text(render_pack(repo, pack, link_root, diff_files))
    (pack.out / "diff.md").write_text(render_diff(repo, pack))
    (pack.out / "review.html").write_text(build_html(repo, pack, diff_files))
    (pack.out / "meta.json").write_text(json.dumps(
        {"number": number, "merge_base": base, "head": head, "base_ref": meta["baseRefName"],
         "head_ref": meta["headRefName"], "url": meta["url"],
         "files": [{"path": c.path, "status": c.status, "added": c.added, "deleted": c.deleted,
                    "unit": c.unit, "interface": is_interface(c)} for c in pack.files]}, indent=1))
    return pack, pack.out / "pack.md", pack.out / "review.html"
