-- Run once on the existing PostgreSQL database before deploying this release.
DO $$ BEGIN
    CREATE TYPE value_selection_mode AS ENUM ('automatic', 'professional', 'expert');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS recommended_value DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS selected_value DOUBLE PRECISION;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS value_selection_mode value_selection_mode NOT NULL DEFAULT 'automatic';
ALTER TABLE reports ADD COLUMN IF NOT EXISTS value_deviation_reason TEXT;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS valuation_statistics JSON;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS search_journal JSON;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS report_options JSON DEFAULT '{"include_screenshots": false, "format": "table"}'::json;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS object_photo_files JSON DEFAULT '[]'::json;
ALTER TABLE reports ADD COLUMN IF NOT EXISTS object_listing_url VARCHAR(1000);
CREATE TABLE IF NOT EXISTS value_change_log (
 id UUID PRIMARY KEY, report_id UUID NOT NULL REFERENCES reports(id), user_id UUID NOT NULL REFERENCES users(id),
 previous_value DOUBLE PRECISION, selected_value DOUBLE PRECISION NOT NULL,
 selection_mode value_selection_mode NOT NULL, reason TEXT, created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_value_change_log_report_id ON value_change_log(report_id);
