-- Admin partner-accounting fields.  Safe to run more than once.
ALTER TABLE agency_referrals
    ALTER COLUMN user_id DROP NOT NULL;

ALTER TABLE agency_referrals
    ADD COLUMN IF NOT EXISTS partner_percent DOUBLE PRECISION NOT NULL DEFAULT 10,
    ADD COLUMN IF NOT EXISTS partner_accrued_uah DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS partner_paid_uah DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS is_partner_active BOOLEAN NOT NULL DEFAULT FALSE;
