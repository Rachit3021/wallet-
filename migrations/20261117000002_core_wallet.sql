-- migrate:up
CREATE TYPE account_kind AS ENUM (
  'user_wallet',
  'system_cash_in',
  'system_cash_out',
  'system_fee_revenue'
);

CREATE TYPE transfer_type AS ENUM (
  'top_up',
  'peer_transfer',
  'withdrawal',
  'fee',
  'reversal'
);

CREATE TYPE transfer_status AS ENUM (
  'pending',
  'held',
  'posted',
  'reversed',
  'failed'
);

CREATE TYPE entry_direction AS ENUM ('debit', 'credit');

CREATE TABLE accounts (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  owner_user_id BIGINT REFERENCES users(id) ON DELETE RESTRICT,
  kind          account_kind NOT NULL,
  label         TEXT NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'INR' CHECK (currency = 'INR'),
  balance_paise BIGINT NOT NULL DEFAULT 0,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (owner_user_id IS NOT NULL AND kind = 'user_wallet')
    OR (owner_user_id IS NULL AND kind <> 'user_wallet')
  ),
  CHECK (owner_user_id IS NULL OR balance_paise >= 0)
);

CREATE UNIQUE INDEX uq_accounts_user_label
  ON accounts(owner_user_id, label)
  WHERE owner_user_id IS NOT NULL;

CREATE UNIQUE INDEX uq_accounts_system_kind
  ON accounts(kind)
  WHERE owner_user_id IS NULL;

CREATE TABLE transfers (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  type                transfer_type NOT NULL,
  status              transfer_status NOT NULL DEFAULT 'pending',
  amount_paise        BIGINT NOT NULL CHECK (amount_paise > 0),
  source_account_id   BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  destination_account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  initiated_by_user_id BIGINT REFERENCES users(id) ON DELETE RESTRICT,
  original_transfer_id BIGINT REFERENCES transfers(id) ON DELETE RESTRICT,
  note                TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (source_account_id <> destination_account_id)
);

CREATE TABLE ledger_entries (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id BIGINT NOT NULL REFERENCES transfers(id) ON DELETE RESTRICT,
  account_id  BIGINT NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
  direction   entry_direction NOT NULL,
  amount_paise BIGINT NOT NULL CHECK (amount_paise > 0),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE transfer_status_audit (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id BIGINT NOT NULL REFERENCES transfers(id) ON DELETE RESTRICT,
  from_status transfer_status,
  to_status   transfer_status NOT NULL,
  actor       TEXT NOT NULL,
  changed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
  id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id       BIGINT REFERENCES users(id) ON DELETE RESTRICT,
  key           UUID NOT NULL,
  request_hash  TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'processing'
                CHECK (status IN ('processing', 'completed')),
  response_code INTEGER,
  response_body JSONB,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at  TIMESTAMPTZ
);

CREATE UNIQUE INDEX uq_idempotency_user_key
  ON idempotency_keys(user_id, key) NULLS NOT DISTINCT;

CREATE TABLE risk_rules (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE,
  description     TEXT NOT NULL,
  rule_kind       TEXT NOT NULL CHECK (rule_kind IN ('amount', 'velocity', 'outflow')),
  threshold_paise BIGINT NOT NULL CHECK (threshold_paise > 0),
  window_minutes  INTEGER NOT NULL DEFAULT 0 CHECK (window_minutes >= 0),
  action          TEXT NOT NULL CHECK (action IN ('hold', 'flag')),
  enabled         BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE risk_flags (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  transfer_id  BIGINT NOT NULL REFERENCES transfers(id) ON DELETE RESTRICT,
  rule_id      BIGINT NOT NULL REFERENCES risk_rules(id) ON DELETE RESTRICT,
  action       TEXT NOT NULL CHECK (action IN ('hold', 'flag')),
  label        TEXT CHECK (label IN ('likely_ok', 'review', 'likely_fraud')),
  explanation  TEXT,
  highlighted_note TEXT,
  reviewed     BOOLEAN NOT NULL DEFAULT false,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE reconciliation_runs (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  ended_at    TIMESTAMPTZ,
  status      TEXT NOT NULL CHECK (status IN ('running', 'passed', 'failed')),
  counts      JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE reconciliation_problems (
  id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id       BIGINT NOT NULL REFERENCES reconciliation_runs(id) ON DELETE RESTRICT,
  check_name   TEXT NOT NULL,
  entity_id    BIGINT,
  expected     TEXT,
  actual       TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE job_locks (
  id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name   TEXT NOT NULL UNIQUE,
  locked_at TIMESTAMPTZ,
  locked_by TEXT
);

CREATE FUNCTION reject_ledger_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'ledger entries are immutable (attempted % on id %)', TG_OP, OLD.id
    USING ERRCODE = 'integrity_constraint_violation';
END;
$$;

CREATE TRIGGER ledger_entries_immutable
BEFORE UPDATE OR DELETE ON ledger_entries
FOR EACH ROW EXECUTE FUNCTION reject_ledger_mutation();

CREATE FUNCTION enforce_ledger_balance() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v RECORD;
BEGIN
  SELECT t.amount_paise,
         COALESCE(sum(le.amount_paise) FILTER (WHERE le.direction = 'debit'), 0) AS debits,
         COALESCE(sum(le.amount_paise) FILTER (WHERE le.direction = 'credit'), 0) AS credits
  INTO v
  FROM transfers t
  LEFT JOIN ledger_entries le ON le.transfer_id = t.id
  WHERE t.id = NEW.transfer_id
  GROUP BY t.id, t.amount_paise;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'ledger entry % references a missing transfer', NEW.id;
  END IF;

  IF v.debits <> v.credits OR v.debits <> v.amount_paise OR v.credits <> v.amount_paise THEN
    RAISE EXCEPTION 'transfer % is unbalanced: amount %, debits %, credits %',
      NEW.transfer_id, v.amount_paise, v.debits, v.credits
      USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER ledger_entries_balance_check
AFTER INSERT ON ledger_entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_ledger_balance();

CREATE INDEX idx_ledger_entries_account_time
  ON ledger_entries(account_id, created_at DESC, id DESC);
CREATE INDEX idx_ledger_entries_transfer
  ON ledger_entries(transfer_id);
CREATE INDEX idx_ledger_entries_direction_account_time
  ON ledger_entries(direction, account_id, created_at DESC);
CREATE INDEX idx_transfers_initiator_time
  ON transfers(initiated_by_user_id, created_at DESC, id DESC);
CREATE INDEX idx_transfers_source_time
  ON transfers(source_account_id, created_at DESC, id DESC);
CREATE INDEX idx_transfers_destination_time
  ON transfers(destination_account_id, created_at DESC, id DESC);
CREATE INDEX idx_transfers_status_time
  ON transfers(status, created_at DESC);
CREATE INDEX idx_transfers_original
  ON transfers(original_transfer_id) WHERE original_transfer_id IS NOT NULL;
CREATE INDEX idx_audit_transfer_time
  ON transfer_status_audit(transfer_id, changed_at DESC);
CREATE INDEX idx_risk_flags_created
  ON risk_flags(created_at DESC) WHERE explanation IS NULL;

INSERT INTO accounts(kind, label, balance_paise) VALUES
  ('system_cash_in', 'external cash-in', 0),
  ('system_cash_out', 'external cash-out', 0),
  ('system_fee_revenue', 'fee revenue', 0);

INSERT INTO job_locks(name) VALUES ('reconciliation');

INSERT INTO risk_rules(code, description, rule_kind, threshold_paise, window_minutes, action) VALUES
  ('LARGE_TRANSFER', 'Hold a single transfer at or above the configured amount', 'amount', 500000, 0, 'hold'),
  ('VELOCITY_10M', 'Hold when one source account starts too many transfers in the window', 'velocity', 1000, 10, 'hold'),
  ('DAILY_OUTFLOW', 'Flag when one source account sends too much in the window', 'outflow', 2000000, 1440, 'flag');

-- migrate:down
DROP TRIGGER IF EXISTS ledger_entries_immutable ON ledger_entries;
DROP TRIGGER IF EXISTS ledger_entries_balance_check ON ledger_entries;
DROP FUNCTION IF EXISTS reject_ledger_mutation();
DROP FUNCTION IF EXISTS enforce_ledger_balance();
DROP TABLE IF EXISTS reconciliation_problems;
DROP TABLE IF EXISTS reconciliation_runs;
DROP TABLE IF EXISTS job_locks;
DROP TABLE IF EXISTS risk_flags;
DROP TABLE IF EXISTS risk_rules;
DROP TABLE IF EXISTS idempotency_keys;
DROP TABLE IF EXISTS transfer_status_audit;
DROP TABLE IF EXISTS ledger_entries;
DROP TABLE IF EXISTS transfers;
DROP TABLE IF EXISTS accounts;
DROP TYPE IF EXISTS entry_direction;
DROP TYPE IF EXISTS transfer_status;
DROP TYPE IF EXISTS transfer_type;
DROP TYPE IF EXISTS account_kind;
