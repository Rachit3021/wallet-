# Performance notes

All numbers below are from the fully seeded local dataset (1,000 users,
2,000 wallets, 100,000 transfers, 200,000 ledger entries) on PostgreSQL 16.
The seeder now runs `ANALYZE` after bulk inserts; without it the planner can
make a very different choice.

## Statement page

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT le.id, le.created_at, le.direction, le.amount_paise,
       t.id AS transfer_id, t.type, t.status, t.note,
       a.label, other.label AS other_label
FROM ledger_entries le
JOIN transfers t ON t.id = le.transfer_id
JOIN accounts a ON a.id = le.account_id
LEFT JOIN LATERAL (
  SELECT acc.label
  FROM ledger_entries le2
  JOIN accounts acc ON acc.id = le2.account_id
  WHERE le2.transfer_id = t.id AND le2.id <> le.id
  ORDER BY le2.id LIMIT 1
) other ON true
WHERE a.owner_user_id = 150
ORDER BY le.created_at DESC, le.id DESC
LIMIT 50;
```

Key lines after `ANALYZE`:

```text
Limit ... (actual time=12.013..16.231 rows=50 loops=1)
  Buffers: shared hit=2324
  Index Scan using uq_accounts_user_label on accounts a
  Bitmap Index Scan on idx_ledger_entries_account_time
  Index Scan using idx_ledger_entries_transfer on ledger_entries le2
Execution Time: 16.574 ms
```

Before statistics were refreshed, the same query took **2038.552 ms** and
touched **284,669** buffers. The planner chose `ledger_entries_pkey` for the
lateral counterparty lookup and filtered out ~95,467 rows per entry. This is
why `scripts/seed.py` runs `ANALYZE users, accounts, transfers, ledger_entries`
at the end of a seed.

## Fraud velocity / outflow window

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*)
FROM transfers
WHERE source_account_id = 1905
  AND created_at >= now() - interval '30 days';
```

```text
Aggregate ... (actual time=0.028..0.029 rows=1 loops=1)
  ->  Index Only Scan using idx_transfers_source_time on transfers
        Index Cond: ((source_account_id = 1905) AND (created_at >= ...))
        Heap Fetches: 0
        Buffers: shared hit=4
Execution Time: 0.047 ms
```

`idx_transfers_source_time` makes the velocity and outflow rules index-only for
the hot path. `VELOCITY_10M` uses a 10-minute window and `DAILY_OUTFLOW` a
1440-minute window; both use the same index.

## Reconciliation balance comparison

```sql
EXPLAIN (ANALYZE, BUFFERS)
WITH computed AS (
  SELECT account_id,
         COALESCE(sum(amount_paise) FILTER (WHERE direction='credit'), 0)
         - COALESCE(sum(amount_paise) FILTER (WHERE direction='debit'), 0) AS computed
  FROM ledger_entries
  GROUP BY account_id
)
SELECT a.id, a.balance_paise, COALESCE(c.computed, 0)
FROM accounts a
LEFT JOIN computed c ON c.account_id = a.id
WHERE a.balance_paise <> COALESCE(c.computed, 0);
```

```text
Hash Left Join ... (actual time=33.460..33.619 rows=0 loops=1)
  ->  Seq Scan on accounts a  (rows=2003)
  ->  Hash
        ->  Finalize HashAggregate  (rows=2001)
              ->  Parallel Seq Scan on ledger_entries  (rows=100000 loops=2)
Execution Time: 33.838 ms
```

A full nightly scan of 200,000 ledger rows costs about 34 ms locally and
returns zero rows on a clean seed. The whole reconciliation run, including all
six checks, completes in well under a second at this dataset size.

## Index inventory

- `idx_ledger_entries_account_time (account_id, created_at DESC, id DESC)` —
  statement pages and per-account balance checks.
- `idx_ledger_entries_transfer (transfer_id)` — counterparty lookup and ledger
  integrity checks.
- `idx_ledger_entries_direction_account_time` — direction-filtered statement
  queries.
- `idx_transfers_source_time (source_account_id, created_at DESC, id DESC)` —
  fraud velocity and outflow windows.
- `idx_transfers_destination_time`, `idx_transfers_initiator_time`,
  `idx_transfers_status_time` — common filters and admin views.
- `idx_risk_flags_created` — nightly explanation pass.
