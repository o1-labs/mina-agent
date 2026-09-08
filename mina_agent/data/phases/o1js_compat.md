---
name: o1js_compat
args: mina, base
allowed_tools: Read, Grep, Glob, Bash(git *), Bash(gh *)
disallowed_tools: Edit, Write, NotebookEdit, WebFetch, WebSearch, Agent, Task, Bash(git push --force*), Bash(git push -f*), Bash(git rebase*), Bash(git reset --hard*), Bash(git submodule update*), Bash(gh pr merge*), Bash(gh pr close*), Bash(gh release*), Bash(gh repo delete*)
permission_mode: default
max_turns: 30
max_budget_usd: 5
needs: gh
env: O1JS_REPO
mode: interactive
---
Open a draft pull request in o1-labs/o1js whose only change is pinning the
`src/mina` submodule to `{{mina}}`, so o1js CI builds and tests against that
revision of Mina. The pull request is a CI probe: it is never merged, and
its title says so.

o1js has exactly one submodule, `src/mina` -> MinaProtocol/mina, and its
workflows check out `submodules: recursive`. proof-systems is nested inside
Mina, so moving that one pointer pins proof-systems too. You never touch
proof-systems directly; you do report which commit came along with it.

`$O1JS_REPO` is the local o1js checkout. Work in a temporary worktree of it,
never in the checkout itself -- the user has their own branch checked out
there and it must be left alone.

1. Resolve the Mina revision. `{{mina}}` is either a branch name or a commit:

       gh api repos/MinaProtocol/mina/commits/{{mina}} --jq .sha

   One call answers both; a branch resolves to its HEAD. What comes back is
   the commit CI will build, and every later step uses that full sha.

   If it 404s, stop and report it. o1js's submodule URL is
   MinaProtocol/mina, so CI can fetch only what that repository has: a
   commit that exists just in a local clone, or only on a fork, cannot be
   tested this way. Do not substitute a nearby commit, and do not push
   anything to make it resolve -- say what is missing and let the user push
   it themselves.

   Then record what rides along:

       gh api "repos/MinaProtocol/mina/contents/src/lib/crypto/proof-systems?ref=<sha>" --jq .sha

2. Check the base exists: `gh api repos/o1-labs/o1js/branches/{{base}}`. If
   it 404s, stop. Do not fall back to the default branch; the base was asked
   for explicitly.

3. Make the worktree from the base as GitHub has it, on a new branch:

       git -C "$O1JS_REPO" fetch origin {{base}}
       git -C "$O1JS_REPO" worktree add -b ci/mina-<short-sha> <tmpdir> origin/{{base}}

   Put <tmpdir> under the system temp directory. If that branch name is
   already taken, add a numeric suffix rather than reusing or deleting it.

4. Move the pointer without fetching Mina. The submodule is large and CI
   clones it anyway, so do not initialise it here -- write the gitlink
   straight into the index:

       git -C <tmpdir> update-index --cacheinfo 160000,<sha>,src/mina

   This works even though the commit is not in o1js's object store. Confirm
   with `git -C <tmpdir> ls-files -s src/mina` (mode 160000, the sha you
   resolved) and `git -C <tmpdir> status --short`, which must show exactly
   `M  src/mina` and nothing else. If anything else is modified, stop and
   report it rather than committing it.

5. Commit and push. The commit is the user's: no Co-Authored-By line, no
   "Generated with" line, no trailer of any kind.

       git -C <tmpdir> commit -m "ci: build o1js against mina <short-sha>"
       git -C <tmpdir> push -u origin ci/mina-<short-sha>

6. Open it as a draft, titled so no reviewer mistakes it for work:

       gh pr create --repo o1-labs/o1js --base {{base}} --draft \
         --title "[ci-only] o1js against mina <short-sha> -- do not merge" \
         --body <the body below>

   The body states: that this is a CI probe and not a change to o1js; the
   Mina commit in full, and the branch it came from when `{{mina}}` was a
   branch; the proof-systems commit inherited through it; and that the whole
   diff is one submodule pointer.

7. Remove the worktree whatever happened, so the checkout is left as you
   found it:

       git -C "$O1JS_REPO" worktree remove --force <tmpdir>

Finish with the PR URL, the base branch, the Mina commit (and the branch, if
that is what you were given), and the proof-systems commit it brings. Say
that CI starts by itself from the `pull_request` trigger -- `build` and
`checks` both, drafts included -- and stop. Do not wait for the checks, do
not merge, do not close the pull request.
