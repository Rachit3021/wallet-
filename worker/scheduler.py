import logging
import os

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.db import pool
from app.reconciliation import reconcile

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wallet.worker")

sched = BlockingScheduler(timezone="Asia/Kolkata")


@sched.scheduled_job("interval", seconds=60, id="heartbeat")
def heartbeat() -> None:
    log.info("worker alive")


@sched.scheduled_job(
    CronTrigger(hour=2, minute=0, timezone="Asia/Kolkata"),
    id="nightly-reconciliation",
)
def nightly_reconciliation() -> None:
    try:
        log.info("nightly reconciliation starting")
        result = reconcile(triggered_by="scheduler")
        log.info("nightly reconciliation result: %s", result)
    except Exception:
        log.exception("nightly reconciliation failed")


if __name__ == "__main__":
    if pool.closed:
        pool.open()
    if os.environ.get("RECONCILE_ON_START", "").lower() in ("1", "true", "yes"):
        nightly_reconciliation()
    log.info("worker starting")
    try:
        sched.start()
    finally:
        pool.close()
