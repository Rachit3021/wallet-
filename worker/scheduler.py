import logging
from apscheduler.schedulers.blocking import BlockingScheduler
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")
sched = BlockingScheduler(timezone="Asia/Kolkata")
@sched.scheduled_job("interval", seconds=60)
def heartbeat():
    log.info("worker alive")
if __name__ == "__main__":
    log.info("worker starting")
    sched.start()
