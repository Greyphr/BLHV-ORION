-- /build: `request` now always holds the original feature request (it is what the AI sees on every later change).
-- What was asked of one particular version ("make it blue") is kept separately.
ALTER TABLE builder_builds ADD COLUMN IF NOT EXISTS change_note TEXT;
