"""Shared Redis connection (queue, run store, caches, rate limiter)."""
from __future__ import annotations

import functools

import redis

from .config import settings


@functools.lru_cache(maxsize=1)
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
        health_check_interval=30,
    )
