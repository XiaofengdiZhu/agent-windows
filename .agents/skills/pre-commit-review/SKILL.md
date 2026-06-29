---
name: pre-commit-review
description: Run an independent subagent code review on any code change BEFORE committing it. Use this automatically, every time, before running `git commit` (or `git commit --amend`) on work that changed source or tests — even for a "small" or "obvious" fix, and even if the change already passed tests. Skipping it has repeatedly let real bugs through (reversed coordinates, unbounded memory, wrong metadata). Trigger whenever you are about to stage and commit code, when the user says "commit this", or when you have finished implementing and are moving toward committing.
---

# Pre-commit review

Every code change gets an independent review **before** it is committed — not after. This is non-negotiable in this repo because "fix and commit" has repeatedly shipped bugs that a fresh-eyes pass would have caught:

- A coordinate-parsing fix introduced two *new* silent mis-parses (reversed x/y on `{"y":..,"x":..}`, floats like `500.5` → `500,5`).
- A read-streaming OOM fix let the memory peak stay unbounded for long-line files (the byte cap ran *after* the full `"".join`).
- A line-count heuristic fabricated wrong totals (a 5-line file reported as 98 lines).
- A validation fix silently rejected a documented value (`offset=0` = whole file).

Tests passing does **not** count as review — several of the above passed their tests. Review does.

## When to run this

Run it whenever you're about to commit work that touched source or tests, including:

- A completed bug fix or feature.
- A "small" / "trivial" / "obvious" change — these are exactly the ones that slip through.
- A change that already passed the full test suite.
- The user saying "commit this", "commit", "提交", or equivalent.
- An `--amend` that re-touches code.

The only exception is a change with **no** source/test diff (docs-only, config-only, `gitignore`, lockfile). When in doubt, review.

## How to run it

1. **Don't commit yet.** Stage the work (or just leave it unstaged — the reviewer reads `git diff`).

2. **Dispatch a code-review subagent.** Give it a self-contained prompt — the reviewer does not share your session history.

   **Prefer the `Explore` subagent type** (read-only, reads excerpts rather than whole files). It reliably completes in ~2 minutes. A `general-purpose` reviewer has repeatedly **timed out** (10 min) on this repo — it tends to fan out and scan build artifacts / the whole tree. Only fall back to `general-purpose` if the review needs to *run* code or *edit* a scratch file (Explore can't execute).

   **Critically: scope the prompt.** Name the exact files:lines to read and tell it NOT to do broad searches or scan `target/`, `node_modules/`, build artifacts, or run broad greps. An unscoped review is what times out. A minimal shape:

   ```
   You are doing a focused pre-commit code review. Read-only, "medium" breadth.
   Do NOT scan build artifacts/target/node_modules or run broad greps — read
   ONLY the specific files/functions cited and reason with concrete inputs.

   What was implemented: <1-3 sentence summary, including the bug it fixes>
   The fix mechanism: <how it works, so the reviewer can verify against it>

   Read these specific files/functions:
   - <path:lines> — <what to check there>
   - <path:lines> — <what to check there>

   VERIFY these specific concerns (the high-risk ones) by reading the code and
   reasoning with concrete inputs:
   (a) <e.g. "could the marker leak to IPC?"> (b) <"is the whitelist complete?">
   ...

   Report each concern as VERIFIED-SAFE (one-line evidence), BUG-FOUND (concrete
   failing input + minimal fix), or UNCLEAR. End with "Ready to commit: Yes/No".
   ```

   See the `requesting-code-review` skill (`code-reviewer.md`) for the full template; this is the lightweight version for a single in-flight change.

3. **Receive the review with skepticism, not deference.** Before implementing any suggestion: verify the claim against the actual code/test with a concrete trace or throwaway script. Reviewers have been wrong (one claimed a "capped limit" was misreported as EOF — it wasn't, because of an earlier `min()` that was missed). Confirm-or-refute each finding empirically, then act. Use the `receiving-code-review` skill.

4. **Fix Critical/Important issues**, re-verify, then commit. Note Minor items.

## The two failure modes to watch for

- **"It's small, skip it."** The bugs above were all "small" fixes. Size is not a reason to skip. If anything, small changes invite less scrutiny and need review more.
- **"Tests passed, it's fine."** Tests verify what you thought to test. Review catches what you didn't — including tests that look meaningful but can't fail (the "memory bounded" test that asserted a return-value tautology). When you write a test for a fix, ask: *would this test fail on the old, buggy code?* If not, it proves nothing.

## Quick checklist before committing

- [ ] A subagent reviewed the *current* diff (not an earlier version). Prefer `Explore`; the prompt names exact files:lines and forbids broad scans.
- [ ] Every Critical/Important finding was verified empirically and either fixed or refuted with evidence.
- [ ] New tests were checked to actually fail on the pre-fix code.
- [ ] The reviewer's verdict was "Yes" (or "With fixes" and the fixes are done).

If any box is unchecked, do not commit.
