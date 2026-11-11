"""Work queue on Redis Streams with consumer groups.

Why streams: a message stays *pending* until a worker acknowledges it. If a
worker dies mid-run, another worker reclaims the message after the lease
expires (XAUTOCLAIM), so no run is silently lost. After
``QUEUE_MAX_DELIVERIES`` attempts the message is dead-lettered and the run is
marked failed (never returned as a confident answer).

Streams used:
    stream:research  API -> research agent (and verifier -> research on retry)
    stream:verify    research agent -> verifier agent (the hand-off)
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import redis

from .config import settings
from .redis_client import get_redis

RESEARCH_STREAM = "stream:research"
VERIFY_STREAM = "stream:verify"
DEAD_LETTER_STREAM = "stream:dead-letter"


@dataclass
class Message:
    msg_id: str
    payload: dict
    deliveries: int


class StreamQueue:
    def __init__(self, stream: str, group: str, r: redis.Redis | None = None):
        self.stream = stream
        self.group = group
        self.r = r or get_redis()
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.r.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except redis.ResponseError as exc:  # group already exists
            if "BUSYGROUP" not in str(exc):
                raise

    def publish(self, payload: dict) -> str:
        return self.r.xadd(self.stream, {"data": json.dumps(payload)}, maxlen=100000, approximate=True)

    def _deliveries(self, msg_id: str) -> int:
        info = self.r.xpending_range(self.stream, self.group, min=msg_id, max=msg_id, count=1)
        return int(info[0]["times_delivered"]) if info else 1

    def consume(self, consumer: str, block_ms: int = 2000) -> Message | None:
        """Return one message: first a reclaimed (expired lease) one, else a new one."""
        try:
            res = self.r.xautoclaim(
                self.stream, self.group, consumer, min_idle_time=settings.queue_lease_ms, start_id="0-0", count=1
            )
            claimed = res[1] if len(res) > 1 else []
        except redis.ResponseError:
            claimed = []
        if claimed:
            msg_id, fields = claimed[0]
            if fields:
                return Message(msg_id, json.loads(fields["data"]), self._deliveries(msg_id))
        resp = self.r.xreadgroup(self.group, consumer, {self.stream: ">"}, count=1, block=block_ms)
        if not resp:
            return None
        _, entries = resp[0]
        msg_id, fields = entries[0]
        return Message(msg_id, json.loads(fields["data"]), 1)

    def ack(self, msg_id: str) -> None:
        self.r.xack(self.stream, self.group, msg_id)
        self.r.xdel(self.stream, msg_id)

    def dead_letter(self, msg: Message, reason: str) -> None:
        self.r.xadd(DEAD_LETTER_STREAM, {"data": json.dumps({"reason": reason, **msg.payload})}, maxlen=10000)
        self.ack(msg.msg_id)

    def depth(self) -> int:
        """Messages waiting or in flight for this stream."""
        try:
            return int(self.r.xlen(self.stream))
        except redis.RedisError:
            return -1
