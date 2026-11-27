import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .integrations import ApiError


class Database:
    def __init__(self, parameters):
        self.conn = psycopg.connect(**parameters, autocommit=True, row_factory=dict_row)
        schema = self.execute("SELECT current_schema() AS name").fetchone()["name"]
        self.lock_prefix = f"channelscout:{schema}:"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.conn.close()

    def execute(self, query, values=()):
        return self.conn.execute(query, values)

    def lock_transaction(self, name):
        self.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.lock_prefix + name,))

    def migrate(self, paths):
        with self.conn.transaction():
            self.lock_transaction("migrate")
            self.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
            for path in sorted(paths):
                if not self.execute("SELECT 1 FROM schema_migrations WHERE name=%s", (path.name,)).fetchone():
                    self.execute(path.read_text(encoding="utf-8"))
                    self.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))

    @contextmanager
    def job(self, name):
        key = self.lock_prefix + name
        locked = self.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (key,)).fetchone()["locked"]
        if not locked:
            raise ApiError("scheduler", "job_already_running")
        run_id = uuid4()
        report = {}
        status = "ok"
        try:
            self.execute(
                """UPDATE job_runs SET finished_at=now(),status='interrupted',
                   report=jsonb_build_object('error','previous_worker_lost')
                   WHERE job=%s AND status='running'""", (name,),
            )
            self.execute("INSERT INTO job_runs (id, job, status) VALUES (%s,%s,'running')", (run_id, name))
            yield report
            if report.get("deferred") or report.get("errors"):
                status = "partial"
        except BaseException as error:
            status = "failed"
            report["error"] = error.code if isinstance(error, ApiError) else type(error).__name__
            raise
        finally:
            try:
                self.execute("UPDATE job_runs SET finished_at=now(),status=%s,report=%s WHERE id=%s",
                             (status, Jsonb(report), run_id))
            finally:
                self.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))

    def reserve(self, bucket, limit):
        day = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
        if limit <= 0:
            raise ApiError("youtube", "daily_quota")
        result = self.execute(
            """INSERT INTO api_budget (quota_day,bucket,used) VALUES (%s,%s,1)
               ON CONFLICT (quota_day,bucket) DO UPDATE SET used=api_budget.used+1
               WHERE api_budget.used < %s RETURNING used""", (day, bucket, limit),
        ).fetchone()
        if not result:
            raise ApiError("youtube", "daily_quota")

    def keywords(self, records):
        enabled = {row["id"] for row in records}
        with self.conn.transaction():
            previous = {row["id"] for row in self.execute("SELECT id FROM keywords WHERE enabled").fetchall()}
            self.execute("UPDATE keywords SET enabled=false WHERE enabled AND NOT (id=ANY(%s))",
                         (list(enabled),))
            for row in records:
                self.execute(
                    """INSERT INTO keywords (id,niche,query,mode,target_format,criteria)
                       VALUES (%(id)s,%(niche)s,%(query)s,%(mode)s,%(target_format)s,%(criteria)s)
                       ON CONFLICT (id) DO UPDATE SET enabled=true, niche=excluded.niche, query=excluded.query,
                       target_format=excluded.target_format, criteria=excluded.criteria,
                       next_run_at=CASE WHEN NOT keywords.enabled OR keywords.target_format<>excluded.target_format
                                        THEN now() ELSE keywords.next_run_at END""",
                    {**row, "criteria": Jsonb(row.get("criteria", {}))},
                )
            if changed := previous ^ enabled:
                affected = self.execute(
                    "SELECT DISTINCT channel_id FROM channel_matches WHERE keyword_id=ANY(%s)", (list(changed),),
                ).fetchall()
                ids = [entry["channel_id"] for entry in affected]
                self.recompute_selection(ids)
                self.execute(
                    "UPDATE channels SET sheet_revision=sheet_revision+1 WHERE id=ANY(%s)", (ids,),
                )
        return self.execute("SELECT * FROM keywords WHERE enabled AND next_run_at<=now() ORDER BY next_run_at,id").fetchall()

    def schedule_keyword(self, key, due, success):
        self.execute("UPDATE keywords SET next_run_at=%s, failures=CASE WHEN %s THEN 0 ELSE failures+1 END WHERE id=%s",
                     (due, success, key))

    def ingest(self, keyword, rows):
        inserted = set()
        with self.conn.transaction():
            for row in rows:
                current = self.execute("SELECT id FROM channels WHERE id=%s FOR UPDATE", (row["id"],)).fetchone()
                if current is None and row["selection"] == "Qualified":
                    inserted.add(row["id"])
                self.execute(
                    """INSERT INTO channels (id, metadata, metadata_at, selection, selection_reason)
                       VALUES (%s,%s,now(),%s,%s)
                       ON CONFLICT (id) DO UPDATE SET metadata=excluded.metadata, metadata_at=now(),
                         sheet_revision=channels.sheet_revision+1""",
                    (row["id"], Jsonb(row["metadata"]), row["selection"], row["reason"]),
                )
                self.execute(
                    """INSERT INTO channel_matches (channel_id,keyword_id,video_id,selection,selection_reason,location_match)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (channel_id,keyword_id) DO UPDATE
                       SET video_id=excluded.video_id,matched_at=now(),selection=excluded.selection,
                           selection_reason=excluded.selection_reason,location_match=excluded.location_match""",
                    (row["id"], keyword["id"], row["match"], row["selection"], row["reason"],
                     row.get("location_match", "Any location")),
                )
            self.recompute_selection([row["id"] for row in rows])
        return inserted

    def recompute_selection(self, ids):
        if not ids:
            return
        self.execute(
            """WITH best AS (
                   SELECT DISTINCT ON (m.channel_id) m.channel_id,m.selection,m.selection_reason
                   FROM channel_matches m JOIN keywords k ON k.id=m.keyword_id
                   WHERE k.enabled AND m.matched_at>now()-interval '29 days' AND m.channel_id=ANY(%s)
                   ORDER BY m.channel_id,CASE m.selection WHEN 'Qualified' THEN 0 WHEN 'Needs Review' THEN 1 ELSE 2 END,
                            m.matched_at DESC,m.keyword_id
               )
               UPDATE channels c SET selection=COALESCE(best.selection,'Excluded'),
                      selection_reason=COALESCE(best.selection_reason,'no_active_keyword_match'),
                      sheet_revision=c.sheet_revision+1
               FROM unnest(%s::text[]) AS selected(id) LEFT JOIN best ON best.channel_id=selected.id
               WHERE c.id=selected.id AND c.metadata<>'{}'::jsonb AND
                     (c.selection,c.selection_reason) IS DISTINCT FROM
                         (COALESCE(best.selection,'Excluded'),COALESCE(best.selection_reason,'no_active_keyword_match'))""",
            (ids, ids),
        )

    def drafting_queue(self, limit, refresh=False):
        return self.execute(
            """SELECT * FROM channels WHERE selection='Qualified' AND metadata_at>now()-interval '29 days'
               AND (draft_due<=now() OR (%s AND draft_status='Pending Approval'))
               ORDER BY (draft_status='Pending Approval' AND %s) DESC,draft_due,id LIMIT %s""",
            (refresh, refresh, limit),
        ).fetchall()

    def save_draft(self, channel_id, recipient, output, draft_fingerprint, provider, model):
        with self.conn.transaction():
            channel = self.execute("SELECT id FROM channels WHERE id=%s FOR UPDATE", (channel_id,)).fetchone()
            if not channel:
                raise ApiError("drafting", "lead_missing")
            current = self.latest_draft(channel_id)
            if current and current["fingerprint"] == draft_fingerprint:
                return current["id"]
            draft_id = uuid4()
            self.execute(
                """INSERT INTO drafts (id,channel_id,recipient,subject,body,evidence_ids,fingerprint,provider,model,review_token)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (draft_id, channel_id, recipient, output.subject, output.body, Jsonb(output.evidence_ids),
                 draft_fingerprint, provider, model, secrets.token_urlsafe(24)),
            )
            return draft_id

    def latest_draft(self, channel_id):
        return self.execute("SELECT * FROM drafts WHERE channel_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                            (channel_id,)).fetchone()

    def draft_result(self, channel_id, contact, source, contact_status, status, due):
        self.execute(
            """UPDATE channels SET contact_email=%s,contact_source=%s,contact_status=%s,draft_status=%s,
               draft_due=%s,sheet_revision=sheet_revision+1 WHERE id=%s""",
            (contact, source, contact_status, status, due, channel_id),
        )

    def label_format(self, channel_id, content, upload, evidence):
        result = self.execute(
            """UPDATE channels SET reviewed_content_format=%s,reviewed_upload_format=%s,
               format_evidence=%s,sheet_revision=sheet_revision+1 WHERE id=%s""",
            (content, upload, evidence, channel_id),
        )
        if not result.rowcount:
            raise ApiError("review", "lead_missing")

    def expire_metadata(self):
        with self.conn.transaction():
            self.execute(
                """UPDATE channels SET metadata='{}'::jsonb,selection='Needs Review',
                   selection_reason='metadata_expired',draft_status='Needs Review',sheet_revision=sheet_revision+1,
                   contact_email=NULL,contact_source=NULL,contact_status='Unchecked'
                   WHERE metadata<>'{}'::jsonb AND metadata_at<=now()-interval '29 days'""",
            )
            self.execute("""UPDATE channel_matches SET video_id=NULL
                            WHERE video_id IS NOT NULL AND matched_at<=now()-interval '29 days'""")

    def pending_channels(self, limit, *, export=False):
        return self.execute(
            """SELECT c.*, d.id AS draft_id,d.subject,d.body,d.review_token,d.recipient,
                      s.status AS send_status,s.finished_at AS sent_at,
                      COALESCE((SELECT jsonb_agg(jsonb_build_object('niche', k.niche, 'query', k.query,
                      'mode', k.mode,'criteria', k.criteria,'location_match',m.location_match)
                      ORDER BY k.niche,k.query,k.mode) FROM channel_matches m
                      JOIN keywords k ON k.id=m.keyword_id WHERE m.channel_id=c.id
                      AND k.enabled AND m.matched_at>now()-interval '29 days'),'[]'::jsonb) AS matches
               FROM channels c
               LEFT JOIN LATERAL (SELECT * FROM drafts WHERE channel_id=c.id ORDER BY created_at DESC,id DESC LIMIT 1) d ON true
               LEFT JOIN LATERAL (SELECT * FROM send_attempts WHERE channel_id=c.id
                                  ORDER BY (status<>'Rejected') DESC,claimed_at DESC LIMIT 1) s ON true
               WHERE (NOT %s AND c.sheet_synced<c.sheet_revision)
                  OR (%s AND c.selection='Qualified' AND c.metadata_at>now()-interval '29 days'
                      AND c.draft_status='Pending Approval' AND c.contact_status='Verified public contact'
                      AND c.contact_email=d.recipient AND s.status IS NULL)
               ORDER BY c.id LIMIT %s""", (export, export, limit),
        ).fetchall()

    def acknowledge_channel(self, channel_id, revision):
        self.execute("UPDATE channels SET sheet_synced=GREATEST(sheet_synced,%s) WHERE id=%s", (revision, channel_id))

    def pending_runs(self, limit):
        return self.execute(
            "SELECT * FROM job_runs WHERE finished_at IS NOT NULL AND NOT sheet_synced AND job<>'sync-sheets' ORDER BY started_at LIMIT %s",
            (limit,),
        ).fetchall()

    def enable_sender(self, enabled):
        with self.conn.transaction():
            self.lock_transaction("delivery")
            self.execute("UPDATE sender_control SET paused=%s WHERE singleton", (not enabled,))

    def import_approvals(self, approvals):
        with self.conn.transaction():
            self.lock_transaction("delivery")
            self.execute("UPDATE approvals SET revoked_at=now() WHERE revoked_at IS NULL AND NOT (draft_id=ANY(%s))",
                         (list(approvals),))
            for draft_id, decision in approvals.items():
                self.execute(
                    """INSERT INTO approvals (draft_id,eligibility_evidence)
                       SELECT id,%s FROM drafts WHERE id=%s AND review_token=%s
                       ON CONFLICT (draft_id) DO NOTHING""", (decision["evidence"], draft_id, decision["token"]),
                )
                self.execute(
                    """UPDATE approvals SET revoked_at=now() WHERE draft_id=%s AND revoked_at IS NULL
                       AND (eligibility_evidence<>%s OR NOT EXISTS
                            (SELECT 1 FROM drafts WHERE id=%s AND review_token=%s))""",
                    (draft_id, decision["evidence"], draft_id, decision["token"]),
                )

    def eligible(self, draft_id, days):
        return self.execute(
            """SELECT d.* FROM drafts d JOIN approvals a ON a.draft_id=d.id
               JOIN channels c ON c.id=d.channel_id
               WHERE d.id=%s AND a.revoked_at IS NULL AND a.approved_at>now()-(%s*interval '1 day')
               AND c.selection='Qualified' AND c.metadata_at>now()-interval '29 days'
               AND c.contact_email=d.recipient AND c.contact_status='Verified public contact'
               AND c.draft_status='Pending Approval'
               AND d.id=(SELECT latest.id FROM drafts latest WHERE latest.channel_id=c.id
                         ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
               AND NOT EXISTS (SELECT 1 FROM suppressions WHERE normalized_recipient=d.recipient)
               AND NOT EXISTS (SELECT 1 FROM send_attempts s WHERE s.draft_id=d.id OR
                  (s.status<>'Rejected' AND (s.channel_id=d.channel_id OR s.normalized_recipient=d.recipient)))""",
            (draft_id, days),
        ).fetchone()

    def claim(self, draft_id, days, cap):
        with self.conn.transaction():
            self.lock_transaction("delivery")
            if self.execute("SELECT paused FROM sender_control WHERE singleton").fetchone()["paused"]:
                return None
            self.execute("""SELECT c.id FROM channels c JOIN drafts d ON d.channel_id=c.id
                            WHERE d.id=%s FOR UPDATE OF c""", (draft_id,)).fetchone()
            count = self.execute(
                """SELECT count(*) AS count FROM send_attempts
                   WHERE (claimed_at AT TIME ZONE 'UTC')::date=(now() AT TIME ZONE 'UTC')::date"""
            ).fetchone()["count"]
            if count >= cap or not (draft := self.eligible(draft_id, days)):
                return None
            message_id = f"<{draft_id}@channelscout.local>"
            claim = self.execute(
                """INSERT INTO send_attempts (draft_id,channel_id,normalized_recipient,status,message_id)
                   VALUES (%s,%s,%s,'Sending',%s) ON CONFLICT DO NOTHING RETURNING message_id""",
                (draft_id, draft["channel_id"], draft["recipient"], message_id),
            ).fetchone()
            return (draft, message_id) if claim else None

    def finish_send(self, draft_id, status, gmail_id=None, reason=None):
        with self.conn.transaction():
            row = self.execute(
                """UPDATE send_attempts SET status=%s,gmail_id=%s,reason=%s,finished_at=now()
                   WHERE draft_id=%s AND status='Sending' RETURNING channel_id""",
                (status, gmail_id, reason, draft_id),
            ).fetchone()
            if row:
                self.execute("UPDATE channels SET sheet_revision=sheet_revision+1 WHERE id=%s", (row["channel_id"],))

    def expire_claims(self):
        with self.conn.transaction():
            return self.execute(
                """WITH stale AS (
                       UPDATE send_attempts SET status='Needs Review',reason='worker_lost',finished_at=now()
                       WHERE status='Sending' AND claimed_at<now()-interval '10 minutes' RETURNING channel_id
                   )
                   UPDATE channels SET sheet_revision=sheet_revision+1 WHERE id IN (SELECT channel_id FROM stale)""",
            ).rowcount

    def resolve_send(self, draft_id, gmail_id):
        with self.conn.transaction():
            self.lock_transaction("delivery")
            row = self.execute(
                """UPDATE send_attempts SET status=%s,gmail_id=%s,reason='reconciled_by_operator',finished_at=now()
                   WHERE draft_id=%s AND status='Needs Review' RETURNING channel_id""",
                ("Gmail Accepted" if gmail_id else "Rejected", gmail_id, draft_id),
            ).fetchone()
            if not row:
                raise ApiError("sender", "no_uncertain_send_for_draft")
            self.execute("UPDATE channels SET sheet_revision=sheet_revision+1 WHERE id=%s", (row["channel_id"],))

    def suppress(self, recipient):
        with self.conn.transaction():
            self.lock_transaction("delivery")
            self.execute("INSERT INTO suppressions (normalized_recipient) VALUES (%s) ON CONFLICT DO NOTHING",
                         (recipient,))
