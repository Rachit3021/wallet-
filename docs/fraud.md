# Fraud rules and LLM boundary

Fraud thresholds live in `risk_rules`, not in application constants. The worker
and API read the enabled rows on every transfer, so an operator can change a
threshold without a redeploy.

| Code | Kind | Threshold | Window | Action | Meaning |
| --- | --- | --- | --- | --- | --- |
| `LARGE_TRANSFER` | `amount` | 500000 paise | — | `hold` | A single transfer at or above ₹5,000 is parked for review. |
| `VELOCITY_10M` | `velocity` | 1000 transfers | 10 min | `hold` | Too many transfers from one source in ten minutes. |
| `DAILY_OUTFLOW` | `outflow` | 2000000 paise | 1440 min | `flag` | High one-day outflow is reported but not blocked. |

`VELOCITY_10M` is intentionally above the 100-request stampede so the
concurrency acceptance test measures locking rather than fraud holds.

## Posting behaviour

1. `create_transfer` inserts a `pending` transfer and a status audit.
2. `fraud.evaluate` runs the enabled rules inside the same transaction and
   inserts a `risk_flags` row for every fired rule.
3. If any rule has `action = 'hold'`, the transfer becomes `held` and responds
   `202` with the fired flags. No balances move and no ledger rows are written.
4. `POST /transfers/{id}/release` requires another idempotency key, checks that
   the transfer is still `held`, marks flags reviewed, and posts the transfer.
5. A `flag`-only rule leaves the transfer posted but keeps the flag for review.

## Supported indexes

```sql
CREATE INDEX idx_transfers_source_time
  ON transfers(source_account_id, created_at DESC, id DESC);
CREATE INDEX idx_transfers_status_time
  ON transfers(status, created_at DESC);
CREATE INDEX idx_risk_flags_created
  ON risk_flags(created_at DESC) WHERE explanation IS NULL;
```

The velocity and outflow queries filter on `source_account_id` and a recent
time window; `idx_transfers_source_time` supports that lookup. The nightly
explanation pass uses `idx_risk_flags_created` to find only flags that still
need an explanation.

## LLM boundary

`app/llm.py` defines a tiny `LLMClient` protocol with two methods:

- `parse_question(question)` — classify statement questions into one of four
  typed, parameterized SQL queries.
- `explain_flag(rule, recent_transfers)` — produce a short review label and
  explanation.

The default `LLM_PROVIDER=mock` is deterministic and offline. `openai` is
supported through `OPENAI_API_KEY`, `LLM_MODEL`, and optional `LLM_BASE_URL`.

The LLM never builds SQL. It returns a constrained JSON object, which is
validated for query type, ISO dates, and bounded counterparty text before any
query runs. Transfer notes and other user-controlled text are passed to the
explanation prompt as **data only**; the system prompt explicitly instructs the
model to ignore instructions found in that text. Statement answers are always
scoped by the authenticated `user_id` in the SQL `WHERE` clause, so a prompt
injection suffix cannot broaden the query.

Tests cover both directions: an injected question still returns only the
caller's transfer IDs, and a pure instruction-override question is rejected
with `422 UNSUPPORTED_QUESTION`.
