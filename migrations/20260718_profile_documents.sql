-- Run once on the production PostgreSQL database after deploying this release.
-- The files themselves are stored in /var/lib/ocinka/profile-documents.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS profile_document_files JSON DEFAULT '[]'::json;
