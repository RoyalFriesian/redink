"""RQ worker entry point. M1: no jobs are enqueued yet; this exists so
the compose `worker` service has something to run. M2+ will queue Slack
clarification / review jobs here via `rq_queue.enqueue(advance, session_id)`.
"""

from __future__ import annotations

import logging

from redis import Redis
from rq import Queue, Worker

from services.config import settings

log = logging.getLogger(__name__)


def get_queue() -> Queue:
    return Queue("redink", connection=Redis.from_url(settings().redis_url))


def run_worker() -> None:
    logging.basicConfig(level=settings().redink_log_level)
    conn = Redis.from_url(settings().redis_url)
    Worker([Queue("redink", connection=conn)], connection=conn).work(with_scheduler=True)


run = run_worker

if __name__ == "__main__":
    run_worker()
