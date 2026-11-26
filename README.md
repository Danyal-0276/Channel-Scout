# ChannelScout

ChannelScout discovers YouTube channels from keywords in Google Sheets, finds publicly listed business contacts, and drafts outreach for human review. PostgreSQL stores leads, draft versions, approvals, and send history.

**Stack:** Python, LangGraph, local PostgreSQL, Google Sheets, and Windows Task Scheduler. Start with the CLI commands described in the plan. FastAPI becomes useful if a separate UI or webhook needs to call the workflow over HTTP.

## Project layout

```text
Channel-Scout/
├── .env                   # Local values, including secrets
├── .env.example           # Shareable configuration template
├── .gitignore
├── README.md
├── docs/
│   ├── implementation-plan.md
│   └── production-architecture.md
├── migrations/            # Versioned PostgreSQL schema changes
├── src/
│   └── channel_scout/      # Python application and LangGraph workflows
└── tests/                 # Filters, integrations, and approval scenarios
```

This is a folder blueprint. The first milestone in the plan adds the package manifest, CLI, schema, and discovery workflow.

## Local configuration

- Use database **`channel_scout`** on `127.0.0.1:5432`. The local bootstrap assumes the standard `postgres` login. Before scheduling the worker, create a restricted application login and put its credentials in `.env`.
- Fill in `YOUTUBE_API_KEY` and `GOOGLE_SHEETS_ID` for discovery. Share the Sheet with the service account and set `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` to its compact, single-line credentials JSON in `.env`.
- For hosted drafting, set `GROQ_API_KEY`. For local drafting, set `MODEL_PROVIDER=ollama` and `MODEL_NAME=qwen3.5:4b` or `qwen3.5:9b`.
- Leave `OUTREACH_DRY_RUN=true` while building and validating the approval flow. Supply Gmail OAuth credentials only when testing the separate sender. Keep the OAuth refresh token in `.env` and load it only in that process.
- Keep `.env` and credential files out of source control and logs. Change the PostgreSQL password shared in chat and update the local configuration before using the account for anything beyond development.

If the database still needs to be created, start the installed PostgreSQL Windows service and run this once. Enter the local password when prompted.

```powershell
createdb -h 127.0.0.1 -p 5432 -U postgres -W channel_scout
```

[Open the implementation plan](docs/implementation-plan.md).

[Open the production architecture, guardrails, and eval plan](docs/production-architecture.md).

