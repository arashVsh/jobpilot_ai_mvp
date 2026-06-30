# JobPilot AI

JobPilot AI is an agentic job-search assistant. It helps you find public job postings, compare them with your resume, track applications, and draft short outreach emails.

The app is designed for job seekers who want one place to:

- upload a resume
- choose job categories, location, work format, salary, and fit-score filters
- scan public job sources
- review saved jobs
- understand why a job was accepted or rejected
- mark jobs as applied or favorite
- set follow-up reminders
- draft recruiter/company emails
- view monthly application activity reports

JobPilot AI does **not** automatically apply to jobs and does **not** send emails for you. You always review the job and the draft before taking action.

---

## Quick start

### Windows

```bash
cd jobpilot_ai_final
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

### macOS / Linux

```bash
cd jobpilot_ai_final
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app/streamlit_app.py
```

After the app starts, open the local URL shown in the terminal, usually:

```text
http://localhost:8501
```

---

## First-time setup

1. Open the app in your browser.
2. Upload your resume.
3. Fill in your basic profile details in the left sidebar:
   - name
   - email
   - affiliation for email signature
   - portfolio or profile link, if available
4. Choose the job categories you want, such as software development, machine learning, cybersecurity, cloud, help desk, or teaching/tutoring.
5. Choose location and work format.
6. Set your minimum fit score.
7. Add API keys if you have them.
8. Click **Start scanning**.

---

## API keys

API keys are optional, but they improve different parts of the app.

| API key | What it improves |
|---|---|
| SerpAPI | Finds more public job postings from search results |
| Tavily | Researches company details, contact pages, recruiters, and CEO/founder information |
| OpenAI | Improves fit reasoning and email drafting |

You can use the app without API keys, but results will be more limited.

### Remembering API keys

The app can remember API keys on your computer if you select the remember option. Saved keys are stored locally in:

```text
outputs/saved_api_keys.json
```

For personal local use, this is convenient. For public deployment or shared machines, use environment variables or a proper secret manager instead.

Never upload API keys, `.env` files, or the `outputs/` folder to GitHub.

---

## How scanning works

When you click **Start scanning**, the app:

1. Builds targeted job-search queries based on your selected industries and locations.
2. Searches public job sources.
3. Cleans and normalizes job descriptions.
4. Removes jobs that clearly fail your filters, such as wrong location, closed postings, unpaid/volunteer roles, or salary below your selected threshold.
5. Scores the remaining jobs against your uploaded resume.
6. Saves matching jobs into the local database.
7. Logs accepted and rejected jobs so you can debug the search quality.

While scanning is running, the main filters are locked so the scan uses one consistent configuration. Stop the scan before changing filters.

---

## Understanding fit scores

Suggested interpretation:

| Fit score | Meaning |
|---:|---|
| 85–100 | Strong match |
| 70–84 | Good match |
| 55–69 | Possible match |
| Below 55 | Usually weak match |

The default minimum fit score is **75**. If you want more results, lower it to **60–65**.

---

## Saved jobs

The **Saved jobs** tab shows matching jobs that passed your filters.

Each job card shows:

- row number
- title
- company
- location
- work format
- salary, when available
- fit score
- important requirements
- why the job was accepted

Click a job card to expand details under that row.

Inside the expanded details you can see:

- job description
- matched skills
- possible gaps
- resume update suggestions
- company-specific detail, when found
- contact/recruiter/CEO information, when found
- email draft, when contact information is available

---

## Application tracking

For each job, you can:

- mark it as **Applied**
- mark it as **Favorite**
- delete/archive it from the visible list
- open the job posting
- open company/contact pages when available
- set a follow-up reminder after applying

Applied and favorite statuses are saved automatically.

---

## Follow-up reminders

After marking a job as applied, you can choose:

- no follow-up
- follow up in 7 days
- follow up in 10 days

Use the **Follow-ups** tab to see upcoming and due reminders.

---

## Rejected jobs debug

The **Rejected jobs debug** tab shows jobs that were found but not shown in the saved list.

Common rejection reasons include:

- location mismatch
- work-format mismatch
- closed or expired posting
- salary below threshold
- fit score below threshold
- duplicate or already seen job
- API error

This tab helps you understand whether your filters are too strict or whether a source is returning irrelevant jobs.

You can download the rejected-job table as CSV for review.

---

## Agent activity log

The **Agent activity log** tab shows what the system did during scanning.

Example activity:

```text
Search Agent: queried software development jobs in Canada.
Filter Agent: removed jobs outside selected locations.
Fit Agent: scored jobs against the uploaded resume.
Research Agent: looked for company details and contact routes.
Email Agent: drafted outreach for selected jobs.
Tracker Agent: saved job records and application statuses.
```

This makes the agentic workflow visible and easier to explain in a demo.

---

## Activity report

The **Activity report** tab summarizes your job-search activity.

It can show:

- total saved jobs
- total applied jobs
- active jobs not yet applied to
- favorite jobs
- applications by month
- companies you applied to, grouped by count
- role titles you applied to, grouped by count

Reports can be downloaded as CSV.

---

## Local database and backups

JobPilot AI saves job records locally in SQLite:

```text
outputs/jobpilot.sqlite
```

This database stores:

- saved jobs
- archived/deleted jobs
- applied/favorite status
- follow-up reminders
- rejected-job debug records
- agent activity logs

Use **Export / backup saved database** if you want to keep a copy or move your history to another computer.

---

## Recommended search settings

For faster results:

```text
Search speed/depth: Fast
Minimum fit score: 60–75
Research companies during scan: Off
Stop after: 10–30 new jobs
Lookback: 72–168 hours if 24 hours gives too few results
```

For more selective results:

```text
Minimum fit score: 75+
Location: specific country/city
Work format: selected formats only
Salary filter: enabled
```

---

## Privacy and security notes

- The app is intended for local personal use.
- API keys are hidden in the UI, but local saved keys are not a production-grade secret store.
- Do not commit `outputs/`, `.env`, local databases, or saved API-key files to GitHub.
- Review all email drafts before sending.
- Do not use the app to spam recruiters or apply automatically.
- The app uses public job sources and avoids logged-in scraping.

---

## Troubleshooting

### I am getting too few saved jobs

Try:

- lowering the minimum fit score to 60–65
- increasing the lookback window to 72 or 168 hours
- selecting more job categories
- checking the **Rejected jobs debug** tab
- using a SerpAPI key for broader discovery

### Many jobs are rejected for location mismatch

This usually means the search source returned jobs outside your selected location. The app keeps them out of your saved list. Try a broader location or check the rejected table to confirm.

### The scanner seems slow

Use Fast mode, reduce company research during scan, and set a limit such as 10–30 new jobs per scan. Company/contact research can be done after opening a job.

### Old irrelevant jobs are still visible

They may have been saved by an older run. Archive/delete them, or use the Danger Zone to wipe history if you want a fresh start.

---

## What this app is not

JobPilot AI is not a replacement for your judgment. It can help you search, filter, organize, and draft, but you should still verify job postings, company details, contact names, and email text before applying.
