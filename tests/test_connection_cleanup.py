import unittest
from unittest.mock import Mock

from token_dashboard import helios_writer


class ConnectionCleanupTests(unittest.TestCase):
    def test_closes_cached_connection_and_is_idempotent(self):
        connection = Mock()
        helios_writer._tlocal.conn = connection
        helios_writer.close_thread_connection()
        helios_writer.close_thread_connection()
        connection.close.assert_called_once_with()
        self.assertIsNone(helios_writer._tlocal.conn)

    def test_close_error_does_not_retain_connection(self):
        connection = Mock()
        connection.close.side_effect = RuntimeError("synthetic close failure")
        helios_writer._tlocal.conn = connection
        helios_writer.close_thread_connection()
        self.assertIsNone(helios_writer._tlocal.conn)


if __name__ == "__main__":
    unittest.main()
