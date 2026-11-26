ALTER TABLE channels
    ADD COLUMN reviewed_content_format text,
    ADD COLUMN reviewed_upload_format text,
    ADD COLUMN format_evidence text;
