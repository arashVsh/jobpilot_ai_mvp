# JobPilot AI MVP v22

Autonomous job-search assistant for discovering, filtering, saving, and tracking job opportunities from public sources.

## v22 fixes

This version focuses on search quality, UI safety during scans, and debug-table reliability.

### What changed

- **One scan button only**: removed the separate `Scan once now` and `Start background` modes. The app now has one **Start scanning** button and a **Stop scanner** button that appears only while a scanner is active.
- **Filters lock during scanning**: resume/search filters/salary filters are disabled while the scanner is running. This prevents filter changes in the middle of a scan.
- **Smarter SerpAPI queries**: the scanner now queries targeted phrases such as `software developer jobs in Canada remote hybrid` instead of broad/random role phrases.
- **No-key remote sources expanded**: optional no-key remote discovery now checks bounded Remotive and RemoteOK passes.
- **Rejected-job CSV export fixed**: the rejected-job debug tab now has a stable CSV download button and cleaner score display.
- **Fit scoring improved**: the rule-based scorer recognizes a broader set of technical/AI/software/help-desk/teaching signals, reducing misleading 0-score outcomes for related jobs.
- **Still human-in-the-loop**: the app never auto-applies or auto-sends emails.

## Recommended settings for quick results

- Search speed/depth: **Fast** or **Balanced**
- Minimum fit score: **60–75**
- Stop each scan after this many new jobs: **30**
- Research companies during scan: **off**
- Include no-key remote-job sources: **on**
- Lookback: **72–168 hours** if 24 hours gives too few roles

## Run

```bash
cd jobpilot_ai_mvp_v22
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

Mac/Linux:

```bash
cd jobpilot_ai_mvp_v22
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

## API keys

- **SerpAPI**: broad automatic job discovery.
- **Tavily**: company, recruiter, CEO/founder research. Usually run on demand for selected jobs.
- **OpenAI**: email drafting and stronger reasoning.

Saved API keys are stored locally in `outputs/saved_api_keys.json` for convenience only. Do not commit this file or use it as production secret storage.

## Storage

Saved jobs, applied/favorite flags, deleted/archive state, follow-ups, logs, and rejected-job debug records are stored in:

```text
outputs/jobpilot.sqlite
```

Use the database export/import controls to back up or move your history.

## v23 patch notes

This version fixes startup/runtime warnings reported on Windows/Streamlit:

- Removed the `remember_api_keys` widget/session-state conflict by initializing session state before widget creation and not passing a separate default value to the checkbox.
- Replaced deprecated `use_container_width=True` calls with `width="stretch"`.
- Fixed rejected-job debug table serialization by converting the `score` column to a consistent string type before displaying/downloading it.
- The rejected-job CSV download should now work without PyArrow type-conversion errors.
