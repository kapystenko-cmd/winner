-- Run once on the production PostgreSQL database after deploying this release.
-- Evidence files themselves are stored under /var/lib/ocinka/uploads.
ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS e_certificate_files JSON DEFAULT '[]'::json;

ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS location_map_files JSON DEFAULT '[]'::json;
