# Cobbs Dependency-Security Agent

Cobbs is a Python 3.10+ portfolio project that detects vulnerable dependencies,
tries bounded upgrades, and asks a repair agent to fix code only when an
upgrade breaks visible tests. It records package versions, test results,
remaining advisories, verification type, and suspicious-code heuristics.

## Quick start

The project uses the Python standard library for its agent backends. Install
`pytest` to run the offline suite; the pipeline creates temporary environments
and installs repository dependencies plus `pip-audit` when it runs:

```powershell
python -m pip install pytest
python -m pytest -q -p no:cacheprovider tests_agent
python -B -m py_compile cobbsagent.py cobbsagent_m2.py eval_harness.py agent.py
```

Run the deterministic milestone-one pipeline or the iterative milestone-two
pipeline against a repository containing `requirements.txt`:

```powershell
python cobbsagent.py path\to\repo
python cobbsagent_m2.py path\to\repo
```

Milestone two defaults to the ordered backends
`gemini:gemini-3.5-flash-lite,ollama:qwen2.5-coder:7b`. Configure backends with
`--backends` or `COBBSAGENT_BACKENDS`; fallback happens only between repair
attempts. Ollama requests stay on the local backend. Gemini requests are paced
and counted against the Pacific-day budget. Use `--max-requests N` to override
the default daily limit.

Responses are cached in `cache\responses.jsonl`. `--no-cache` disables caching;
`--cache-only` refuses to make a model request on a cache miss. Tests use fake
backends and clocks and do not call model APIs.

## Sandbox and reports

```powershell
python cobbsagent_m2.py --sandbox docker path\to\repo
python cobbsagent_dashboard.py --report report.json --traces traces --output dashboard.html
```

Docker mode runs pytest in a resource-limited container with no inherited host
environment and no network during test execution. Dependency installation
uses a separate bounded container with bridge networking. The pipeline still
uses its normal host venv for pip-audit and installed-version inventory, so
Docker mode isolates tests rather than all package installation; see
[docs/SANDBOX.md](docs/SANDBOX.md). The dashboard is a standalone static HTML
file. It escapes and redacts displayed fields and omits raw prompts, source,
tool contents, and test logs.

Each package is upgraded in up to three rounds using fresh resolution. The
final version, remaining vulnerability IDs, stop reason, tests, and agent
activity are saved in `report.json`; `results_agent.md` contains a readable
summary. Audits target the dependency closure of `requirements.txt`, not the
agent's tooling. Transitive versions reflect the final resolved environment.

The prompt preloads a bounded source snapshot and the failing test output as
untrusted data. File paths are jailed to the sandbox, writes to tests and
requirements are rejected, and successful edits trigger tests. The agent has
a step/token limit; every claimed fix is independently re-tested. Hidden-test
metadata stays out of the model prompt and is applied only after the agent
finishes. AST tampering detection is heuristic, not proof of intent.

## Pull-request workflow

`cobbsagent_pr.py` supports an allow-listed repository workflow. It is dry-run
by default and never merges. The dry-run validates a saved eligible attempt,
applies its patches to a fresh shallow clone, checks changed paths and secrets,
and prints a proposed title, body, branch, and diff. `--apply` is a separate
live action that commits, pushes, and calls the GitHub API after confirmation.
Use it only when you intend those external side effects.

## Offline demo and publishing

```powershell
python demo.py
python demo.py --repo path\to\repo --runs 2
python demo.py --dashboard-only --report report.json --traces traces
python build_publish.py
```

The demo runs the milestone-two repair pipeline against `eval/jwt_breaking` by
default, then builds a dashboard from the resulting report and traces. It may
send requests to the configured model backend and install dependencies; it
does not create a GitHub pull request. Use `--dashboard-only` to render saved
outputs without running repairs. The publish builder copies only entries in
[publish_allowlist.txt](publish_allowlist.txt), then scans staged files for
credential patterns. The allow-list intentionally includes
`eval/expected.json` and the `jwt_weak_tests` hidden test for a reproducible
evaluation bundle; those files are exposed to publish recipients. The agent
sandbox still excludes them. The builder refuses to overwrite an existing
publish directory. The project uses the MIT License, attributed to the
repository owner. No `publish/.gitignore` is generated.

See [DEMO.md](DEMO.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), and
[docs/SANDBOX.md](docs/SANDBOX.md) for workflow details.
