"""Provision a least-privilege login for an existing local ChannelScout schema."""

import re
import secrets
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from channel_scout.config import ConfigError, Settings
from channel_scout.db import Database


ROLE = "channel_scout_worker"


def main():
    root = Path(__file__).resolve().parents[1]
    settings = Settings(root)
    path = root / ".env"
    original = path.read_text(encoding="utf-8-sig")
    parameters = settings.postgres(maintenance=True)
    if parameters["host"] not in {"127.0.0.1", "::1", "localhost"} or parameters["dbname"] != "channel_scout":
        raise ConfigError("This helper is only for the local channel_scout database.")
    candidate = root / f".env.scout-{secrets.token_hex(6)}.tmp"
    role_committed = False
    try:
        with Database(parameters) as db:
            if db.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (ROLE,)).fetchone():
                raise ConfigError("The worker login already exists. Use its existing credentials or ask the DBA to rotate it.")
            if not db.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL AS ready").fetchone()["ready"]:
                raise ConfigError("Apply the migrations first.")
            password = secrets.token_urlsafe(32)
            updates = {
                "POSTGRES_USER": ROLE, "POSTGRES_PASSWORD": password,
                "POSTGRES_MIGRATION_USER": parameters["user"],
                "POSTGRES_MIGRATION_PASSWORD": parameters["password"],
            }
            document = original
            for name, value in updates.items():
                if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    raise ConfigError("A PostgreSQL setting needs manual quoting in .env. No changes were made.")
                pattern = re.compile(rf"^{name}=.*$", re.M)
                if len(pattern.findall(document)) > 1:
                    raise ConfigError(f"Duplicate {name} setting")
                document = pattern.sub(lambda _: f"{name}={value}", document) if pattern.search(document) else (
                    document.rstrip("\n") + f"\n{name}={value}\n"
                )
            candidate.write_text(document, encoding="utf-8")
            verifier = db.conn.pgconn.encrypt_password(password.encode(), ROLE.encode(), b"scram-sha-256").decode()
            with db.conn.transaction():
                db.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                    sql.Identifier(ROLE), sql.Literal(verifier)))
                db.execute(sql.SQL("GRANT CONNECT ON DATABASE channel_scout TO {}").format(sql.Identifier(ROLE)))
                db.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(ROLE)))
                db.execute(sql.SQL("GRANT SELECT ON public.schema_migrations, public.sender_control TO {}").format(sql.Identifier(ROLE)))
                db.execute(sql.SQL(
                    """GRANT SELECT, INSERT, UPDATE ON public.keywords, public.channels, public.channel_matches,
                       public.approvals, public.send_attempts, public.suppressions, public.api_budget,
                       public.job_runs TO {}"""
                ).format(sql.Identifier(ROLE)))
                db.execute(sql.SQL("GRANT SELECT, INSERT ON public.drafts TO {}").format(sql.Identifier(ROLE)))
            role_committed = True
            with Database({**parameters, "user": ROLE, "password": password}) as worker:
                worker.execute("SELECT count(*) FROM channels").fetchone()
                worker.execute("SELECT paused FROM sender_control WHERE singleton").fetchone()
            if path.read_text(encoding="utf-8-sig") != original:
                raise ConfigError(".env changed during provisioning. The new login exists. Recover its credential from the prepared ignored file.")
            candidate.replace(path)
    finally:
        # Preserve the staged password if provisioning committed but the file could not be replaced.
        if candidate.exists() and not role_committed:
            candidate.unlink()
    print("Restricted PostgreSQL worker login verified and saved in .env.")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as error:
        sys.exit(str(error))
    except (psycopg.Error, OSError) as error:
        sys.exit(f"Database provisioning failed ({type(error).__name__})")
