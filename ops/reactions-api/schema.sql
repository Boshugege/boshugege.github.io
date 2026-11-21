-- pnc-reactions: one row per visitor and target ("观测" on a post or note).
-- target is "/posts/<slug>.html" or "note:<id>"; visitor_hash is
-- HMAC(VISITOR_SALT, IPv4 address or IPv6 /64), so the raw address is never
-- stored and the primary key is the "once per visitor" rule.
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS reactions (
  target       TEXT NOT NULL,
  visitor_hash TEXT NOT NULL,
  created_at   TEXT NOT NULL,              -- ISO 8601, UTC
  PRIMARY KEY (target, visitor_hash)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS reactions_by_time ON reactions (created_at);
-- "Which of these has this visitor observed?" on every page view.
CREATE INDEX IF NOT EXISTS reactions_by_visitor ON reactions (visitor_hash, target);
