"""Per-client rate limiting (requirement D7).

A fixed one-minute window per client id in Redis. This is a cost control:
one run costs several model calls, so a client looping against the API
could otherwise burn a free-tier quota or a budget in minutes.
"""
from __future__ import annotations

import time

from ..common.redis_client import get_redis


def check_rate_limit(client: str, limit_per_min: int) -> tuple[bool, int]:
    if limit_per_min <= 0:
        return True, 0
    window = int(time.time() // 60)
    key = f"ratelimit:{client}:{window}"
    r = get_redis()
    pipe = r.pipeline()
    pipe.incr(key)
    pipe.expire(key, 70)
    count, _ = pipe.execute()
    if int(count) > limit_per_min:
        return False, 60 - int(time.time() % 60)
    return True, 0
