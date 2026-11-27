# ChannelScout operator runbook

## Local setup

Run commands from the repository root in PowerShell. A Windows virtual environment is already present in the development workspace. For a fresh checkout, create one first.

```powershell
python -m venv .venv
$scoutPython = '.\.venv\Scripts\python.exe'
& $scoutPython -m pip install -e '.[test]'
```

Create the local `.env` from `.env.example` if needed. Set the PostgreSQL values for the local database, the restricted YouTube API key, the spreadsheet ID and a Groq key or local Ollama provider. Set `CHANNELSCOUT_OFFER` to the actual offer, audience, and call to action. With the sample keyword already on the Sheet, one discovery run exercises the first full integration.

```powershell
& $scoutPython -m channel_scout.cli migrate
& $scoutPython scripts\provision_database.py
& $scoutPython scripts\configure_sheets.py 'C:\path\to\downloaded-service-account-key.json'
& $scoutPython -m channel_scout.cli doctor
& $scoutPython -m channel_scout.cli setup-sheets
& $scoutPython -m channel_scout.cli discover
& $scoutPython -m channel_scout.cli sync-sheets
& $scoutPython -m channel_scout.cli draft
& $scoutPython -m channel_scout.cli sync-sheets
```

The database provisioning helper is one-time and limited to a local `channel_scout` database. User-level job commands use its restricted login. Keep migration and Gmail credentials available only to trusted operators. For a live deployment, inject credentials per process or deployment and give the sender its own process identity. Download the service-account key under the Google Cloud project and grant that account Editor on this specific spreadsheet. The helper installs the key without displaying it.

`setup-sheets` validates the existing header prefix and completes `Leads` and `Run Log`. Keep those two tabs as application-owned projections. Use filter views to sort and filter during processing. Structural changes or physical sorts during projection writes can race with Google Sheets row updates. The job verifies stable IDs and checks for duplicate IDs. Keep the original `Keywords` headers `Niche`, `Keyword`, `Enabled`. Optional `Search Mode` and `Target Format` fields are accepted. Channel search is the default.

The same command installs the optional country, city or town, subscriber range, and location policy fields and the lead priority colors. See [location targeting and outreach review](location-and-outreach.md). Use `draft --refresh` to recheck current pending drafts after updating messaging guidance.

The 90-day activity calculation is disabled by default while YouTube policy treatment and which formats count are confirmed. The Recent Activity column displays available source publication dates in that mode. Content and upload formats remain `Unknown` when the available evidence does not support a label. `CHANNELSCOUT_CONTACT_DOMAINS` accepts a comma-separated list of verified creator-owned HTTPS hostnames for optional off-site contact enrichment. The YouTube description route runs without this list.

To enter reviewed format labels, run `label-format <channel-id> --content 'Podcast, Interview' --upload 'Live replay' --evidence 'Creator-provided format confirmation'` through the CLI. The next sync projects both labels separately.

## Scheduling

After the manual smoke run succeeds, register the desired jobs from PowerShell 7. Each registered job runs every 15 minutes when the Windows account is signed in and the PC is available. Unchanged keywords refresh daily. Read failed job information in Task Scheduler, `logs/`, or `channel-scout job-status`. The Sheet's `Run Log` carries the projected run counts.

```powershell
.\scripts\register-jobs.ps1 -IncludeDiscoveryAndSync -IncludeDrafting
```

The registration script does not register the sender. Run and verify sender dry-run manually first.

## Owner approval and sending

1. The Sheet owner configures an `Approvals` tab with columns `Draft ID`, `Review Token`, `Decision`, and `Eligibility Evidence`. Protect the **whole tab** with strict protection, only the designated owner as editor, and no unprotected ranges. Confirm the service account can read the tab and cannot edit it.
2. Review the exact contact email, subject, and body in `Leads`. Copy the displayed draft ID and review token into the protected tab. Enter `Approve` and the recipient's opt-in or request in `Eligibility Evidence`. To revoke, replace `Approve` with `Revoke` or clear the row. A revoked or expired version requires a new draft. A published email address is contact evidence. Gmail outreach also needs eligibility for the planned message. [Gmail program policies](https://support.google.com/mail/answer/16734397)
3. Run `send-approved` while dry-run is enabled. It imports approval state and reports eligible counts. Use your own mailbox for the first live test and set the required Gmail OAuth settings. A real send requires `OUTREACH_DRY_RUN=false`, the `--live` argument, and the database sender switch. Confirm all staging and reviewer checks before using `sender-enable`.
4. Run `sync-sheets` to publish statuses. `Sent` means Gmail accepted the submission. An uncertain outcome remains blocked from automatic resubmission.

For a reviewed edit, run `revise-draft <channel-id> <reviewed-json-file>`. Use JSON fields `status` set to `draft`, `subject`, `body`, and `evidence_ids` from the current evidence. The revision uses the currently verified contact and publishes a new approval token on the next sync.

To obtain Gmail OAuth credentials, download a **Desktop app** OAuth client JSON from your Google Cloud project. Then run the interactive helper and authorize the correct sending mailbox when Google opens. This only configures OAuth. The DB switch and dry-run remain in their current states.

```powershell
& $scoutPython -m pip install -e '.[gmail-setup]'
& $scoutPython scripts\configure_gmail.py 'C:\path\to\desktop-oauth-client.json' --sender 'you@example.com'
```

```powershell
& $scoutPython -m channel_scout.cli send-approved
& $scoutPython -m channel_scout.cli sender-pause
& $scoutPython -m channel_scout.cli send-status
```

Use `suppress address@example.com` to record an opt-out. Use `send-status` to find a pending attempt's RFC 5322 Message-ID. Check the sending mailbox and reconcile it with `resolve-send <draft-id> --gmail-id <provider-message-id>` or `resolve-send <draft-id> --confirmed-not-sent` only when there is affirmative evidence the previous attempt was never submitted. A missing search result is insufficient evidence. Pause before a database restore and reconcile mail submitted since the backup before resuming.

## Test and evaluate

```powershell
$env:CHANNELSCOUT_TEST_POSTGRES = '1'
& $scoutPython -m pytest -q
& $scoutPython scripts\evaluate.py
```

Tests run on disposable schemas. The evaluation script calls your configured model on synthetic facts and reports machine-checked schema, abstention, banned phrases, and latency. Add `--review-file logs\synthetic-review.json` after creating the ignored `logs` directory to save synthetic drafts for human review. Use `--target fallback` after configuring an Ollama fallback. Confirm factual grounding and suitability with a representative labeled holdout before live outreach. Model evaluation never calls Gmail.
