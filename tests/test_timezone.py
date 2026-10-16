import os
import tempfile
import unittest

from token_dashboard.db import connect, daily_token_breakdown, init_db


class TimezoneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, 'usage.db')
        init_db(self.db)

    def test_daily_totals_follow_local_midnight_across_dst(self):
        with connect(self.db) as c:
            c.executemany(
                'INSERT INTO messages (uuid, session_id, project_slug, type, timestamp, input_tokens) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                [(str(i), 's', 'p', 'assistant', ts, tokens) for i, (ts, tokens) in enumerate([
                    ('2026-03-08T07:59:59Z', 1),
                    ('2026-03-08T08:00:00Z', 2),
                    ('2026-03-09T06:59:59Z', 4),
                    ('2026-03-09T07:00:00Z', 8),
                ])],
            )
            c.execute("INSERT INTO plan (k, v) VALUES ('timezone', 'America/Los_Angeles')")
            c.commit()
        rows = daily_token_breakdown(self.db)
        self.assertEqual([(r['day'], r['input_tokens']) for r in rows], [
            ('2026-03-07', 1), ('2026-03-08', 6), ('2026-03-09', 8),
        ])
        self.assertEqual(sum(r['input_tokens'] for r in rows), 15)

    def test_day_range_handles_short_and_long_days(self):
        from datetime import datetime, timezone
        from token_dashboard.dates import day_range
        for now, expected in [
            ('2026-03-08T19:00:00+00:00', ('2026-03-08T08:00:00+00:00', '2026-03-09T07:00:00+00:00')),
            ('2026-11-01T19:00:00+00:00', ('2026-11-01T07:00:00+00:00', '2026-11-02T08:00:00+00:00')),
        ]:
            self.assertEqual(day_range(1, 'America/Los_Angeles', datetime.fromisoformat(now)), expected)
        start, end = day_range(7, 'Asia/Kolkata', datetime(2026, 10, 16, 12, tzinfo=timezone.utc))
        self.assertEqual(start, '2026-10-09T18:30:00+00:00')
        self.assertEqual(end, '2026-10-16T18:30:00+00:00')

    def test_timezone_validation_does_not_replace_saved_value(self):
        from token_dashboard.db import get_timezone, set_timezone
        self.assertIsNone(get_timezone(self.db))
        set_timezone(self.db, 'America/Los_Angeles')
        for invalid in ['Not/A_Zone', '../UTC', 'Factory', 'localtime', 'posix/UTC', 'right/UTC', '', None, 123, [], {}]:
            with self.assertRaises(ValueError):
                set_timezone(self.db, invalid)
        self.assertEqual(get_timezone(self.db), 'America/Los_Angeles')
