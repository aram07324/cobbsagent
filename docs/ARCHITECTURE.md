# Architecture

The command-line scripts remain compatibility entry points. `agent.py` and
`cobbsagent.py` re-export the agent and deterministic milestone-one APIs from
`cobbs/`; `cobbsagent_m2.py` additionally forwards legacy monkeypatch points
used by the offline tests.

## Agent and milestone one

- `prompt.py` owns the model defaults, bounded-loop limits, and repair prompt.
- `safety.py` builds the preloaded task, enforces sandbox path and protected-file
  rules, and dispatches the repository tools.
- `agent_loop.py` parses model actions and applies the step/token budgets.
- `gemini.py` and `ollama.py` implement provider HTTP calls. `cache.py` handles
  response caching and selects one backend for each repair attempt; `budget.py`
  persists Gemini request counts in Pacific time.
- `tracing.py` writes JSONL events.
- `sandbox.py`, `versions.py`, and `audit.py` implement the deterministic
  milestone-one sandbox, package version operations, and requirements-scoped
  audit. Audit tooling runs in a sibling venv, separate from repository
  dependencies; failed or invalid audit output is never treated as a clean
  result. Its optional Docker test mode installs dependencies in a bounded
  container and executes pytest offline with a read-only repository mount.
- `m1_cli.py` is the milestone-one command-line workflow.

## Milestone two

- `m2_pipeline.py` creates a clean baseline sandbox, installs the declared
  requirements, runs visible tests, and audits only their resolved closure.
- `m2_attempts.py` handles each vulnerable package. In each bounded round it
  picks the maximum minimum fix, copies the current repository into a fresh venv,
  installs from scratch, runs tests, and re-audits after the round. A clean audit
  stops the package; newly disclosed fixable advisories start the next round.
- `m2_repair.py` handles agent runs for an upgrade whose tests fail.
- `m2_finalize.py` builds the final package record, patch, and cost summary.
- `m2_resume.py` restores safe snapshots and contains version/requirements helpers.
- `m2_metadata.py`, `m2_tampering.py`, and `m2_scoring.py` isolate evaluation
  metadata, the added-code AST heuristic, and post-run scoring. Evaluation data
  remains outside the agent prompt and sandbox.
- `m2_diff.py` and `m2_reporting.py` produce patches, JSON reports, and Markdown.
- `m2_cli.py` parses options, builds the backend chain, and runs repositories.

For a test-breaking upgrade, `m2_repair.py` gives the agent a preloaded failure
log and source snapshot. `safety.py` jails file paths and rejects writes to
requirements and test files; it also neutralizes repository text in untrusted
tags. After an edit, the tool dispatcher runs visible tests. A claimed fix is
copied back only after an independent test run, then the package is audited in
the fresh round environment. Scoring runs after the agent is finished; hidden
tests are staged outside the sandbox at that point, and evaluation metadata is
never placed in the prompt.

## Safety enforcement

- `sandbox.py` excludes `.git`, local virtual environments, generated caches,
  `eval_hidden`, and `eval/expected.json` from repository copies.
- `safety.py` enforces the sandbox path jail, evaluation-data path denial,
  protected-file writes, and untrusted-file/log wrappers.
- `agent_loop.py`, `budget.py`, `gemini.py`, and `cache.py` enforce step/token,
  daily request, pacing, cache, and backend-fallback boundaries.
- `m2_repair.py` performs independent test verification; `m2_scoring.py` checks
  hidden tests and the canary only after agent completion.
- `m2_tampering.py` is a heuristic signal for suspicious added code, not proof
  of intent. It is separate from the path and protected-file enforcement.
- `cobbsagent_pr.py` (Phase C) applies allow-list, eligibility, path, secret, and
  confirmation checks before any stage, commit, push, or GitHub API operation.
- `dashboard.py` renders a static, offline HTML summary from report fields and
  selected trace metadata; raw prompts, source, tool results, and test logs are
  deliberately omitted.
- `build_publish.py` copies only the reviewed `publish_allowlist.txt` entries
  after path validation and a credential-pattern scan. It never cleans or
  overwrites an existing destination.

`tests_agent/test_pipeline_golden.py` captures a normalized fake end-to-end
repair result. Tests inject fake runners and backends; they do not call provider
APIs or install packages.
