import hashlib
import math
import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    last_activity: float
    reservations: int = 0
    failures: int = 0
    lockout_until: float = 0.0
    lockout_level: int = 0


class _Lease:
    def __init__(self, limiter, keys):
        self._limiter = limiter
        self._keys = keys
        self._finished = False


class LoginAttemptLimiter:
    def __init__(
        self,
        clock=time.monotonic,
        max_entries=2048,
        threshold=5,
        base_lockout=60,
        max_lockout=900,
        retention=1800,
    ):
        if max_entries < 1 or threshold < 1:
            raise ValueError("max_entries and threshold must be positive")
        if (
            not math.isfinite(base_lockout)
            or not math.isfinite(max_lockout)
            or not math.isfinite(retention)
            or base_lockout <= 0
            or max_lockout < base_lockout
            or retention <= 0
        ):
            raise ValueError("durations must be positive and max_lockout >= base_lockout")
        self._clock = clock
        self._max_entries = max_entries
        self._threshold = threshold
        self._base_lockout = base_lockout
        self._max_lockout = max_lockout
        self._retention = retention
        self._buckets = {}
        self._lock = threading.RLock()

    def begin(self, peer, username):
        now = self._clock()
        keys = (self._key("peer", peer), self._key("username", username))
        with self._lock:
            self._purge(now)
            existing = {key: self._buckets.get(key) for key in keys}
            missing = sum(bucket is None for bucket in existing.values())
            if len(self._buckets) + missing > self._max_entries:
                return None, 60

            retry = 0
            for bucket in existing.values():
                if bucket is not None:
                    self._expire_lockout(bucket, now)
                    if bucket.lockout_until > now:
                        retry = max(retry, self._retry_after(bucket.lockout_until, now))
                    elif bucket.reservations + bucket.failures >= self._threshold:
                        retry = max(retry, 1)
            if retry:
                return None, retry

            for key, bucket in existing.items():
                if bucket is None:
                    bucket = _Bucket(last_activity=now)
                    self._buckets[key] = bucket
                bucket.reservations += 1
                bucket.last_activity = now
            return _Lease(self, keys), 0

    def finish(self, lease, success):
        if not isinstance(lease, _Lease) or lease._limiter is not self:
            return
        with self._lock:
            if lease._finished:
                return
            lease._finished = True
            now = self._clock()
            for key in lease._keys:
                bucket = self._buckets.get(key)
                if bucket is None:
                    continue
                self._expire_lockout(bucket, now)
                if bucket.reservations:
                    bucket.reservations -= 1
                if success:
                    bucket.failures = 0
                    bucket.lockout_until = 0.0
                    bucket.lockout_level = 0
                else:
                    bucket.failures += 1
                    if bucket.failures >= self._threshold:
                        lockout = self._lockout_duration(bucket.lockout_level)
                        bucket.lockout_until = now + lockout
                        if lockout < self._max_lockout:
                            bucket.lockout_level += 1
                bucket.last_activity = now

    def _key(self, kind, identity):
        value = str(identity).encode("utf-8", "surrogatepass")
        digest = hashlib.sha256(kind.encode("ascii") + b"\0" + value).digest()
        return kind, digest

    def _purge(self, now):
        expired = [
            key
            for key, bucket in self._buckets.items()
            if (
                bucket.reservations == 0
                and bucket.lockout_until <= now
                and now - bucket.last_activity >= self._retention
            )
        ]
        for key in expired:
            del self._buckets[key]

    def _expire_lockout(self, bucket, now):
        if bucket.lockout_until and bucket.lockout_until <= now:
            bucket.lockout_until = 0.0
            bucket.failures = 0

    @staticmethod
    def _retry_after(deadline, now):
        return max(1, int(math.ceil(deadline - now)))

    def _lockout_duration(self, level):
        duration = self._base_lockout
        while level and duration < self._max_lockout:
            duration = min(self._max_lockout, duration * 2)
            level -= 1
        return duration
