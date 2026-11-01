import threading
import unittest

from token_dashboard.login_limits import LoginAttemptLimiter


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def fail_attempt(limiter, peer="peer", username="user"):
    lease, retry = limiter.begin(peer, username)
    if lease is None or retry != 0:
        raise AssertionError("attempt was not admitted")
    limiter.finish(lease, False)


class LoginAttemptLimiterTests(unittest.TestCase):
    def test_threshold_expiry_and_escalation(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock)
        for _ in range(5):
            fail_attempt(limiter)
        lease, retry = limiter.begin("peer", "user")
        self.assertIsNone(lease)
        self.assertEqual(retry, 60)
        clock.advance(60)
        for _ in range(5):
            fail_attempt(limiter)
        lease, retry = limiter.begin("peer", "user")
        self.assertIsNone(lease)
        self.assertEqual(retry, 120)
        clock.advance(120)
        lease, retry = limiter.begin("peer", "user")
        self.assertIsNotNone(lease)
        self.assertEqual(retry, 0)

    def test_lockout_deadline_is_not_extended_by_blocked_requests(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock)
        for _ in range(5):
            fail_attempt(limiter)
        clock.advance(10)
        self.assertEqual(limiter.begin("peer", "user")[1], 50)
        clock.advance(10)
        self.assertEqual(limiter.begin("peer", "user")[1], 40)

    def test_peer_and_username_buckets_are_independent(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock)
        for _ in range(5):
            fail_attempt(limiter, peer="peer-a", username="user-a")
        lease, retry = limiter.begin("peer-b", "user-a")
        self.assertIsNone(lease)
        self.assertEqual(retry, 60)
        lease, retry = limiter.begin("peer-a", "user-b")
        self.assertIsNone(lease)
        self.assertEqual(retry, 60)
        lease, retry = limiter.begin("peer-b", "user-b")
        self.assertIsNotNone(lease)
        self.assertEqual(retry, 0)

    def test_success_resets_failures_without_erasing_other_reservations(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock, threshold=2)
        first, _ = limiter.begin("peer", "user")
        second, _ = limiter.begin("peer", "user")
        self.assertIsNone(limiter.begin("peer", "user")[0])
        limiter.finish(first, True)
        third, retry = limiter.begin("peer", "user")
        self.assertIsNotNone(third)
        self.assertEqual(retry, 0)
        limiter.finish(second, False)
        limiter.finish(second, False)
        lease, retry = limiter.begin("peer", "user")
        self.assertIsNone(lease)
        self.assertEqual(retry, 1)
        limiter.finish(third, False)
        lease, retry = limiter.begin("peer", "user")
        self.assertIsNone(lease)
        self.assertEqual(retry, 60)

    def test_concurrent_admission_never_exceeds_threshold(self):
        limiter = LoginAttemptLimiter()
        barrier = threading.Barrier(20)
        leases = []
        lock = threading.Lock()

        def admit():
            barrier.wait()
            lease, _ = limiter.begin("peer", "user")
            if lease is not None:
                with lock:
                    leases.append(lease)

        threads = [threading.Thread(target=admit) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(leases), 5)
        for lease in leases:
            limiter.finish(lease, True)

    def test_capacity_fails_closed_and_cleanup_reclaims_only_inactive_buckets(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock, max_entries=2, retention=10)
        first, _ = limiter.begin("peer-a", "user-a")
        self.assertIsNotNone(first)
        self.assertIsNone(limiter.begin("peer-b", "user-b")[0])
        self.assertEqual(limiter.begin("peer-a", "user-b")[1], 60)
        limiter.finish(first, True)
        clock.advance(10)
        second, retry = limiter.begin("peer-b", "user-b")
        self.assertIsNotNone(second)
        self.assertEqual(retry, 0)

    def test_validation_requires_positive_finite_durations_and_order(self):
        invalid = ((0, 60, 1800), (60, 0, 1800), (60, 59, 1800), (60, 60, 0))
        for base, maximum, retention in invalid:
            with self.subTest(base=base, maximum=maximum, retention=retention):
                with self.assertRaises(ValueError):
                    LoginAttemptLimiter(
                        base_lockout=base,
                        max_lockout=maximum,
                        retention=retention,
                    )
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    LoginAttemptLimiter(base_lockout=value, max_lockout=900, retention=1800)

    def test_max_lockout_caps_repeated_windows(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock, base_lockout=1, max_lockout=4)
        expected = (1, 2, 4, 4, 4, 4, 4)
        for duration in expected:
            for _ in range(5):
                fail_attempt(limiter)
            self.assertEqual(limiter.begin("peer", "user")[1], duration)
            clock.advance(duration)

    def test_retention_does_not_purge_active_lockout(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(
            clock=clock,
            max_entries=2,
            retention=10,
            base_lockout=60,
            max_lockout=60,
        )
        for _ in range(5):
            fail_attempt(limiter)
        clock.advance(20)
        self.assertEqual(limiter.begin("other-peer", "other-user")[1], 60)

    def test_retention_does_not_purge_active_reservations(self):
        clock = Clock()
        limiter = LoginAttemptLimiter(clock=clock, max_entries=2, retention=10)
        lease, _ = limiter.begin("peer", "user")
        clock.advance(20)
        self.assertEqual(limiter.begin("other-peer", "other-user")[1], 60)
        limiter.finish(lease, True)
        clock.advance(10)
        replacement, retry = limiter.begin("other-peer", "other-user")
        self.assertIsNotNone(replacement)
        self.assertEqual(retry, 0)


if __name__ == "__main__":
    unittest.main()
