-- Per-account client-encrypted settings blob (currently: the user's AI
-- provider choice, model selection and API key).
--
-- The column holds a "v1:..." blob encrypted in the browser under the user's
-- DEK — the same key that protects chats and research boards — so the server
-- stores an API key it cannot read. Because the blob is wrapped under the DEK
-- and not the password-derived KEK, a password change (which only re-wraps the
-- DEK) leaves it valid without re-encryption.
--
-- Guests keep their settings in localStorage and never touch this column.

ALTER TABLE users ADD COLUMN IF NOT EXISTS enc_settings TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS settings_updated_at TIMESTAMPTZ;
