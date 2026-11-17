-- Inject exactly three corruption classes for reconciliation acceptance testing.
--
-- Run against a freshly reset database:
--   docker compose exec -T postgres psql -U wallet -d wallet -f /dev/stdin < scripts/corrupt.sql
-- The API container can run it with:
--   python -c "from scripts.corrupt import run; run()"
--
-- session_replication_role=replica bypasses the immutability trigger so the test
-- can damage posted ledger rows. The trigger remains enabled for application
-- traffic. Postgres superuser privileges are required for the role switch.

BEGIN;
SET LOCAL session_replication_role = replica;

DO $$
DECLARE
  cash_in BIGINT;
  u1 BIGINT;
  u2 BIGINT;
  u3 BIGINT;
  w1 BIGINT;
  w2 BIGINT;
  w3 BIGINT;
  t1 BIGINT;
  t2 BIGINT;
  t3 BIGINT;
BEGIN
  SELECT id INTO cash_in
  FROM accounts
  WHERE owner_user_id IS NULL AND kind = 'system_cash_in';

  INSERT INTO users(name, email, phone, kyc_status)
  VALUES ('Corrupt One', 'corrupt-one@example.com', '+919900000001', 'verified')
  RETURNING id INTO u1;
  INSERT INTO users(name, email, phone, kyc_status)
  VALUES ('Corrupt Two', 'corrupt-two@example.com', '+919900000002', 'verified')
  RETURNING id INTO u2;
  INSERT INTO users(name, email, phone, kyc_status)
  VALUES ('Corrupt Three', 'corrupt-three@example.com', '+919900000003', 'verified')
  RETURNING id INTO u3;

  INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
  VALUES (u1, 'user_wallet', 'main', 0) RETURNING id INTO w1;
  INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
  VALUES (u2, 'user_wallet', 'main', 0) RETURNING id INTO w2;
  INSERT INTO accounts(owner_user_id, kind, label, balance_paise)
  VALUES (u3, 'user_wallet', 'main', 0) RETURNING id INTO w3;

  INSERT INTO transfers(type, status, amount_paise, source_account_id, destination_account_id, note)
  VALUES ('top_up', 'posted', 1000, cash_in, w1, 'corruption target 1')
  RETURNING id INTO t1;
  INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
  VALUES (t1, cash_in, 'debit', 1000), (t1, w1, 'credit', 1000);
  UPDATE accounts SET balance_paise = balance_paise - 1000 WHERE id = cash_in;
  UPDATE accounts SET balance_paise = balance_paise + 1000 WHERE id = w1;

  INSERT INTO transfers(type, status, amount_paise, source_account_id, destination_account_id, note)
  VALUES ('top_up', 'posted', 1000, cash_in, w2, 'corruption target 2')
  RETURNING id INTO t2;
  INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
  VALUES (t2, cash_in, 'debit', 1000), (t2, w2, 'credit', 1000);
  UPDATE accounts SET balance_paise = balance_paise - 1000 WHERE id = cash_in;
  UPDATE accounts SET balance_paise = balance_paise + 1000 WHERE id = w2;

  INSERT INTO transfers(type, status, amount_paise, source_account_id, destination_account_id, note)
  VALUES ('top_up', 'posted', 1000, cash_in, w3, 'corruption target 3')
  RETURNING id INTO t3;
  INSERT INTO ledger_entries(transfer_id, account_id, direction, amount_paise)
  VALUES (t3, cash_in, 'debit', 1000), (t3, w3, 'credit', 1000);
  UPDATE accounts SET balance_paise = balance_paise - 1000 WHERE id = cash_in;
  UPDATE accounts SET balance_paise = balance_paise + 1000 WHERE id = w3;

  -- Fault 1: one ledger amount no longer matches its transfer amount.
  UPDATE ledger_entries SET amount_paise = 1001 WHERE transfer_id = t1 AND direction = 'debit';

  -- Fault 2: the stored cache is wrong while the ledger is internally balanced.
  UPDATE accounts SET balance_paise = 999 WHERE id = w2;

  -- Fault 3: a posted transfer loses its credit leg.
  DELETE FROM ledger_entries WHERE transfer_id = t3 AND direction = 'credit';
END $$;

RESET session_replication_role;
COMMIT;
