import threading
import unittest
from unittest.mock import Mock, patch

from token_dashboard.scan_control import ScanBusy, ScanCoordinator


class ScanControlTests(unittest.TestCase):
    def test_run_is_singleflight_and_cleans_after_success(self):
        cleanup = Mock()
        coordinator = ScanCoordinator(cleanup)

        def operation():
            with self.assertRaises(ScanBusy):
                coordinator.run(lambda: None)
            with self.assertRaises(ScanBusy):
                coordinator.start(lambda: None)
            return 7

        self.assertEqual(coordinator.run(operation), 7)
        cleanup.assert_called_once_with()
        self.assertEqual(coordinator.run(lambda: 8), 8)

    def test_operation_and_cleanup_failures_release_slot(self):
        coordinator = ScanCoordinator(Mock(side_effect=RuntimeError("cleanup")))
        with self.assertRaises(RuntimeError):
            coordinator.run(lambda: None)
        coordinator._cleanup = Mock()
        with self.assertRaises(ValueError):
            coordinator.run(Mock(side_effect=ValueError("operation")))
        self.assertEqual(coordinator.run(lambda: 3), 3)

    def test_async_admission_precedes_worker_and_releases_on_finish(self):
        entered = threading.Event()
        release = threading.Event()
        coordinator = ScanCoordinator(Mock())

        def operation():
            entered.set()
            release.wait(2)

        worker = coordinator.start(operation)
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(ScanBusy):
                coordinator.start(lambda: None)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(coordinator.run(lambda: 4), 4)

    def test_thread_creation_failure_releases_slot(self):
        coordinator = ScanCoordinator(Mock())
        with patch("token_dashboard.scan_control.threading.Thread", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                coordinator.start(lambda: None)
        self.assertEqual(coordinator.run(lambda: 5), 5)


if __name__ == "__main__":
    unittest.main()
