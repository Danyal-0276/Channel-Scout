import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg

from .config import ConfigError, Settings
from .db import Database
from .domain import CHANNEL_ID, fingerprint, normalize_email, validate_draft
from .integrations import ApiError, Gmail, GoogleToken, Model, Sheets, YouTube
from .outreach import run_sender
from .sheets_sync import sync_sheets
from .workflows import evidence_packet, run_discovery, run_drafting


def parser():
    parser = argparse.ArgumentParser(prog="channel-scout")
    parser.add_argument("--project-dir", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "migrate",
        "doctor",
        "setup-sheets",
        "discover",
        "draft",
        "sync-sheets",
        "sender-pause",
        "sender-enable",
        "send-status",
        "job-status",
    ):
        commands.add_parser(name)
    send = commands.add_parser("send-approved")
    send.add_argument("--live", action="store_true")
    suppress = commands.add_parser("suppress")
    suppress.add_argument("recipient")
    label = commands.add_parser("label-format")
    label.add_argument("channel_id")
    label.add_argument("--content", default="")
    label.add_argument("--upload", default="")
    label.add_argument("--evidence", required=True)
    revise = commands.add_parser("revise-draft")
    revise.add_argument("channel_id")
    revise.add_argument("reviewed_json", type=Path)
    resolve = commands.add_parser("resolve-send")
    resolve.add_argument("draft_id", type=UUID)
    result = resolve.add_mutually_exclusive_group(required=True)
    result.add_argument("--gmail-id")
    result.add_argument("--confirmed-not-sent", action="store_true")
    return parser


def sheets_client(settings, client):
    return Sheets(
        client, settings.require("GOOGLE_SHEETS_ID"), GoogleToken.sheets(settings)
    )


def model_clients(settings, client):
    provider = settings.get("MODEL_PROVIDER", "groq").lower()
    if provider not in {"groq", "ollama"}:
        raise ConfigError("MODEL_PROVIDER must be groq or ollama")
    default_name = "openai/gpt-oss-20b" if provider == "groq" else "qwen3.5:4b"
    local_url = settings.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    if provider == "ollama" or settings.flag("OLLAMA_FALLBACK_ENABLED"):
        url = httpx.URL(local_url)
        if (
            url.scheme != "http"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ConfigError("OLLAMA_BASE_URL must use the local loopback address")
    primary = Model(
        client,
        provider,
        settings.get("MODEL_NAME", default_name),
        key=settings.require("GROQ_API_KEY") if provider == "groq" else "",
        local_url=local_url,
    )
    fallback = None
    if provider == "groq" and settings.flag("OLLAMA_FALLBACK_ENABLED"):
        fallback = Model(
            client,
            "ollama",
            settings.get("OLLAMA_FALLBACK_MODEL", "qwen3.5:4b"),
            local_url=local_url,
        )
    return primary, fallback


def doctor(db, settings):
    current = db.execute(
        """SELECT current_database() AS database,to_regclass('schema_migrations') IS NOT NULL AS migrated,
           (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS admin"""
    ).fetchone()
    ready = {
        name: bool(settings.get(name))
        for name in (
            "YOUTUBE_API_KEY",
            "GROQ_API_KEY",
            "GOOGLE_SHEETS_ID",
            "GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON",
            "CHANNELSCOUT_OFFER",
            "GMAIL_OAUTH_REFRESH_TOKEN",
        )
    }
    current["paused"] = (
        db.execute("SELECT paused FROM sender_control WHERE singleton").fetchone()[
            "paused"
        ]
        if current["migrated"]
        else True
    )
    print(
        json.dumps(
            {
                **current,
                "credentials_configured": ready,
                "dry_run": settings.flag("OUTREACH_DRY_RUN", True),
            }
        )
    )


def revise_draft(db, channel_id, path):
    with db.conn.transaction():
        current = db.execute(
            """SELECT * FROM channels WHERE id=%s AND selection='Qualified'
               AND metadata_at>now()-interval '29 days' AND contact_status='Verified public contact' FOR UPDATE""",
            (channel_id,),
        ).fetchone()
        if not current or not current["contact_email"]:
            raise ConfigError(
                "The lead needs a current verified contact before revising the draft"
            )
        evidence = evidence_packet(current["metadata"].get("description", ""))
        output = validate_draft(path.read_text(encoding="utf-8-sig"), evidence)
        if output.status != "draft":
            raise ConfigError("A revised draft must have draft status")
        key = (
            fingerprint(channel_id, current["contact_email"], evidence)
            + ":"
            + str(uuid4())
        )
        draft_id = db.save_draft(
            channel_id, current["contact_email"], output, key, "human", "reviewed"
        )
        db.draft_result(
            channel_id,
            current["contact_email"],
            current["contact_source"],
            current["contact_status"],
            "Pending Approval",
            max(current["draft_due"], datetime.now(timezone.utc)),
        )
    print(f"New draft version {draft_id} is pending approval.")


def scheduled(args, db, settings, client):
    command = args.command
    limit = settings.integer("CHANNELSCOUT_BATCH_LIMIT", 10, 1, 100)
    if command == "discover":
        sheets = sheets_client(settings, client)
        quotas = {
            "search": settings.integer("YOUTUBE_SEARCH_BUDGET", 90, 1, 10000),
            "general": settings.integer("YOUTUBE_GENERAL_BUDGET", 9000, 1, 1000000),
        }
        deadline = time.monotonic() + 540

        def reserve(bucket):
            if time.monotonic() > deadline:
                raise ApiError("youtube", "job_deadline")
            db.reserve(bucket, quotas[bucket])

        youtube = YouTube(client, settings.require("YOUTUBE_API_KEY"), reserve)
        with db.job(command) as report:
            run_discovery(
                db,
                sheets,
                youtube,
                settings.flag("ENABLE_ACTIVITY_CLASSIFICATION"),
                limit,
                report,
            )
    elif command == "draft":
        primary, fallback = model_clients(settings, client)
        offer = settings.require("CHANNELSCOUT_OFFER")
        hosts = {
            host.strip().lower()
            for host in settings.get("CHANNELSCOUT_CONTACT_DOMAINS").split(",")
            if host.strip()
        }
        with db.job(command) as report:
            run_drafting(db, primary, fallback, offer, hosts, limit, report)
    elif command == "sync-sheets":
        with db.job(command) as report:
            sync_sheets(db, sheets_client(settings, client), limit, report)
    else:
        live = args.live
        if live and settings.flag("OUTREACH_DRY_RUN", True):
            raise ConfigError("Live sending is disabled by OUTREACH_DRY_RUN")
        sheets = sheets_client(settings, client)
        gmail = None
        if live:
            admin = db.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone()["rolsuper"]
            if admin:
                raise ConfigError("Live sending requires a restricted PostgreSQL role")
            gmail = Gmail(
                client,
                GoogleToken.gmail(settings),
                normalize_email(settings.require("GMAIL_SENDER_EMAIL")),
            )
        cap = settings.integer("OUTREACH_DAILY_CAP", 5, 1, 25)
        days = settings.integer("OUTREACH_APPROVAL_DAYS", 7, 1, 30)
        with db.job(command) as report:
            run_sender(db, sheets, gmail, live, cap, days, report)
    counts = {key: value for key, value in report.items() if key != "keywords"}
    print(json.dumps(counts, ensure_ascii=False))
    return 1 if report.get("errors") or report.get("uncertain") else 0


def main(argv=None):
    args = parser().parse_args(argv)
    # LangGraph state contains contact and draft data. Keep automatic tracing disabled.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    settings = Settings(args.project_dir.resolve())
    command = args.command
    try:
        maintenance = command in {
            "migrate",
            "sender-enable",
            "sender-pause",
            "resolve-send",
        }
        with (
            Database(settings.postgres(maintenance=maintenance)) as db,
            httpx.Client(
                timeout=httpx.Timeout(12, connect=5),
                trust_env=False,
                follow_redirects=False,
                headers={
                    "User-Agent": "ChannelScout/0.1",
                    "Accept": "application/json",
                },
            ) as client,
        ):
            if command == "migrate":
                paths = list((settings.root / "migrations").glob("*.sql"))
                if not paths:
                    raise ConfigError("Migration folder is missing or empty")
                db.migrate(paths)
                print("ChannelScout schema is current.")
            elif command == "doctor":
                doctor(db, settings)
            elif command == "setup-sheets":
                sheets_client(settings, client).setup_headers()
                print("ChannelScout Sheet headers are current.")
            elif command in {"sender-enable", "sender-pause"}:
                if command == "sender-enable":
                    sheets_client(settings, client).verified_approvals()
                db.enable_sender(command == "sender-enable")
                print("Sender control updated.")
            elif command == "send-status":
                print(
                    json.dumps(
                        db.execute(
                            """SELECT draft_id,message_id,gmail_id,status,reason,claimed_at,finished_at
                       FROM send_attempts ORDER BY claimed_at DESC LIMIT 25"""
                        ).fetchall(),
                        default=str,
                    )
                )
            elif command == "job-status":
                print(
                    json.dumps(
                        db.execute(
                            """SELECT DISTINCT ON (job) job,status,started_at,finished_at,
                       report - 'keywords' AS counts FROM job_runs ORDER BY job,started_at DESC"""
                        ).fetchall(),
                        default=str,
                    )
                )
            elif command == "suppress":
                db.suppress(normalize_email(args.recipient))
                print("Recipient suppression saved.")
            elif command in {"label-format", "revise-draft"}:
                if not CHANNEL_ID.fullmatch(args.channel_id):
                    raise ConfigError("Invalid YouTube channel ID")
                if command == "label-format":
                    if not 5 <= len(args.evidence) <= 1000 or not (
                        args.content or args.upload
                    ):
                        raise ConfigError(
                            "Provide a format label and its review evidence"
                        )
                    db.label_format(
                        args.channel_id,
                        args.content.strip()[:120] or None,
                        args.upload.strip()[:120] or None,
                        args.evidence.strip(),
                    )
                    print("Format review saved for the next Sheets sync.")
                else:
                    revise_draft(db, args.channel_id, args.reviewed_json)
            elif command == "resolve-send":
                if args.gmail_id is not None and (
                    not args.gmail_id.strip() or len(args.gmail_id) > 256
                ):
                    raise ConfigError("Invalid Gmail message ID")
                db.resolve_send(args.draft_id, args.gmail_id)
                print("Send outcome recorded.")
            else:
                return scheduled(args, db, settings, client)
        return 0
    except (ConfigError, ApiError) as error:
        print(str(error), file=sys.stderr)
    except (psycopg.Error, ValueError, OSError, KeyError) as error:
        print(f"{command} failed ({type(error).__name__})", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
