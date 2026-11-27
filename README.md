<p align="center">
  <img src="assets/branding/channel-scout-mascot.png" width="200" alt="ChannelScout mascot, a purple robot detective with a magnifying glass">
</p>

<h1 align="center">ChannelScout Agent</h1>

<p align="center">
  <strong>Find YouTube channels. Review leads. Approve outreach.</strong>
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&amp;logoColor=white">
  <img alt="LangGraph" src="https://img.shields.io/badge/LangGraph-1C3C3C">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?logo=postgresql&amp;logoColor=white">
  <img alt="Google Sheets" src="https://img.shields.io/badge/Google%20Sheets-34A853?logo=googlesheets&amp;logoColor=white">
</p>

<p align="center">
  <a href="#scout-how-it-works">How it works</a> ·
  <a href="#scout-quick-start">Quick start</a> ·
  <a href="#scout-approval-and-sending">Approval and sending</a> ·
  <a href="#scout-project-docs">Project docs</a>
</p>

ChannelScout Agent turns keywords in Google Sheets into a deduplicated list of YouTube channels. It looks for publicly listed business contacts and uses channel evidence to draft personalized outreach. LangGraph runs discovery and drafting, PostgreSQL holds the records, and Google Sheets is where you review results.

<a name="scout-how-it-works"></a>
<h2 align="center">How it works</h2>

1. **Enter keywords.** In `Keywords`, add a niche, keyword, and enabled flag. Channel search is the default. Optional video search finds matching videos and traces them back to their channels.
2. **Filter and deduplicate.** ChannelScout checks up to three result pages per keyword. It keeps channels with 1,000–1,000,000 reported subscribers, excludes reported countries `IN`, `PK`, and `BD`, and keeps channels with no country listed. A channel appears once, with its matching keywords preserved.
3. **Review leads.** The `Leads` tab keeps selection, format, and contact status separate. Green means **Qualified**, yellow means **Needs Review**, and light red means **Excluded** when an excluded row is displayed. `Run Log` distinguishes raw search hits, unique channels, qualified leads, and contacts found.
4. **Review drafts.** ChannelScout keeps the contact source alongside the lead and leaves the address blank when no public business contact is found. Groq or local Ollama drafts from sourced channel details and the offer you configure.

The optional 90-day activity filter is disabled by default. Channels with hidden or unavailable subscriber counts go to review.

Add a country, city or town, or custom subscriber bounds to a keyword row to narrow or broaden that search. Leads show the target area, match evidence category, and priority. Green rows have a verified public contact and a draft, blue rows are qualified and need more work, and amber rows need review. [Location and priority rules](docs/location-and-outreach.md)

<a name="scout-quick-start"></a>
<h2 align="center">Quick start</h2>

You’ll need **Python 3.12+**, PostgreSQL, a YouTube Data API key, and a Google Sheet shared with a Sheets service account. For drafting, choose a Groq API key or a local Ollama model.

From the repo root in PowerShell:

```powershell
python -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -e '.[test]'
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item '.env.example' '.env' }
```

Use [the environment template](.env.example) to configure the database, YouTube, Sheets, and your model. Set `CHANNELSCOUT_OFFER` to what you’re offering creators and the next step you want the draft to ask for. Keep secrets in your ignored `.env`. The [operator runbook](docs/operations.md) walks through the database migration, restricted login, and service-account setup.

Once those are ready, run one manual cycle:

```powershell
& $python -m channel_scout.cli doctor
& $python -m channel_scout.cli setup-sheets
& $python -m channel_scout.cli discover
& $python -m channel_scout.cli draft
& $python -m channel_scout.cli sync-sheets
```

On Windows, register the discovery, drafting, and sync jobs with `scripts/register-jobs.ps1`. Registered jobs run every 15 minutes while the Windows user is signed in and the PC is available. New or changed keywords are picked up on the next discovery run. Existing keywords refresh daily as quota allows. [Scheduling instructions](docs/operations.md#scheduling)

<a name="scout-approval-and-sending"></a>
<h2 align="center">Approval and sending</h2>

- In the protected `Approvals` tab, approve the exact recipient, subject, and body using the draft ID and review token. An edit creates a new draft version that needs fresh approval.
- Gmail sending uses a dedicated command and starts **paused** in **dry-run** mode. Live sending requires Gmail OAuth, explicit live configuration, a current approval, and recipient eligibility.
- The sender records submission attempts in PostgreSQL to prevent duplicates. An uncertain Gmail response goes to **Needs Review** for manual reconciliation.

[Owner approval and recovery instructions](docs/operations.md#owner-approval-and-sending)

<h2 align="center">Repository layout</h2>

| Path | What’s there |
| --- | --- |
| [`assets/branding/`](assets/branding/) | Original mascot PNG for the README or a future interface |
| [`src/channel_scout/`](src/channel_scout/) | CLI, LangGraph workflows, integrations, and approval checks |
| [`migrations/`](migrations/) | Versioned PostgreSQL schema |
| [`scripts/`](scripts/) | Credential setup, Windows scheduling, and model evaluation |
| [`tests/`](tests/) and [`evals/`](evals/) | Regression tests and synthetic drafting cases |
| [`docs/`](docs/) | Setup, architecture, and operating guides |

<h2 align="center">Tests and evaluations</h2>

Run `python -m pytest -q` in the virtual environment. PostgreSQL integration tests use disposable schemas and can be enabled with `CHANNELSCOUT_TEST_POSTGRES=1`. The optional `scripts/evaluate.py` command calls the configured model with synthetic inputs to check structured output, abstention, and latency. [Test and evaluation instructions](docs/operations.md#test-and-evaluate)

<a name="scout-project-docs"></a>
<h2 align="center">Project docs</h2>

- [Operator runbook](docs/operations.md) for credentials, local setup, scheduling, and recovery.
- [Location targeting and outreach review](docs/location-and-outreach.md) for search criteria, lead colors, and Excel export.
- [Production architecture](docs/production-architecture.md) for workflows, guardrails, fallbacks, and release gates.
- [Implementation plan](docs/implementation-plan.md) for project structure and database design.
