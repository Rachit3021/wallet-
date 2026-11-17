# Five-minute demo

Video: [`demo.mp4`](demo.mp4) (5:00, 1280x720, silent slide walkthrough).

Regenerate it with:

```bash
powershell -ExecutionPolicy Bypass -File scripts/make_demo_video.ps1
```

## Timeline

| Time | Topic | What to show in the live terminal |
| --- | --- | --- |
| 0:00–0:30 | Title and architecture | `docker compose ps` |
| 0:30–1:00 | One-command startup | `docker compose up -d --build`, then `curl localhost:8000/health` |
| 1:00–1:30 | Users and wallets | Run `python -m scripts.smoke` |
| 1:30–2:00 | Money movement | Point at the `top_up` and `peer_transfer` responses |
| 2:00–2:30 | Read models | `GET /users/{id}/balance` and `GET /users/{id}/statement` |
| 2:30–3:00 | Idempotency | Replay a POST with the same key, then change the body |
| 3:00–3:30 | Stampede | `python -m scripts.stampede --runs 1` |
| 3:30–4:00 | Fraud | Send 500000 paise, show `held`, then call `/release` |
| 4:00–4:30 | Reconciliation | `python -m scripts.corrupt`, then `POST /admin/reconciliation/run` |
| 4:30–5:00 | Takeaways | Immutable ledger, row locks, idempotency, ledger-derived truth |

## Live narration outline

1. **Start.** `docker compose up` brings up Postgres, dbmate migrations, the
   API, and the scheduler. `/health` proves the pool and schema are ready.
2. **Create.** The smoke script creates two verified users and one wallet each.
   Every POST carries a UUID `Idempotency-Key`.
3. **Post.** A top-up credits a wallet from `system_cash_in`. A peer transfer
   moves paise between two wallets. Each transfer writes exactly one debit and
   one credit; the deferred trigger checks equality at commit.
4. **Read.** The balance endpoint uses the cached projection; the statement
   reads immutable ledger entries with keyset pagination. Both amounts are
   integer paise.
5. **Retry safely.** Replaying the same key and body returns the saved response
   with no second transfer. Reusing the key with a different body returns 422.
6. **Contend.** The stampede script fires 100 concurrent 10-paise spends from a
   500-paise wallet. Exactly 50 post, 50 fail with insufficient funds, and the
   books stay balanced.
7. **Review.** A 500000-paise transfer fires `LARGE_TRANSFER`, is held, and
   does not move money until an operator releases it with a fresh idempotency
   key.
8. **Reconcile.** The corruption script bypasses the immutability trigger in a
   test-only superuser session and injects three faults. The nightly job uses a
   job lock and a `REPEATABLE READ` snapshot and reports exactly three problems.
9. **Close.** The ledger is the source of truth, balances are a fast projection,
   and correctness comes from locks, constraints, idempotency, and reconciliation.
