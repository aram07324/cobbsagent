# Dependency-repair demo

The live demo runs the milestone-two pipeline and then renders its report and
traces. By default it uses `eval\jwt_breaking`, so it may install dependencies
and send requests to the configured model backend. It does not create a GitHub
pull request.

1. Run the default case or choose another repository:

   ```powershell
   python demo.py
   python demo.py --repo path\to\repo --runs 2
   ```

2. Open `dashboard.html` in a browser. The page is self-contained and omits
   prompts, raw source, test logs, and tool contents.

To view existing local outputs without repairs or API requests:

   ```powershell
   python demo.py --dashboard-only --report report.json --traces traces \
      --output dashboard.html
   ```

The model backend and request budget can be configured with the same options
as `cobbsagent_m2.py`, for example `--backends gemini:gemini-3.5-flash-lite`.
Gemini requires `GEMINI_API_KEY` in the environment. The normal response cache
and daily request budget still apply; use `--no-cache` to bypass cached model
responses during repair attempts.
Dashboard-only mode never starts the repair pipeline. The PR workflow remains a
separate action; `cobbsagent_pr.py --apply` can commit, push, and create a live
GitHub pull request after confirmation.
