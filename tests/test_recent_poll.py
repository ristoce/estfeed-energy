"""Recent consumption must not be delayed by successful historical work."""
import asyncio
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from test_manager import Subject, manager, model


class RecentPollSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def subject(self, age, pending=True):
        sub = Subject()
        now = int(datetime.now(timezone.utc).timestamp()) // 3600 * 3600
        first = now - 100 * manager.DAY
        sub.data.update(first=first, cursor=now - 14 * manager.DAY, discovered=now,
                        recent_at=now - age,
                        pending=[[first, first + manager.DAY]] if pending else [],
                        audited_at=now)
        async def fetch(start, end, publish=True):
            sub.calls.append((start, end, publish))
        sub.fetch_window = fetch
        return sub, now

    async def test_clearing_last_retry_does_not_restart_six_hour_poll_timer(self):
        sub, now = self.subject(5 * 3600)
        with patch.object(manager.time, 'monotonic', return_value=1000):
            await sub.run()
        self.assertFalse(sub.data['pending'])
        self.assertEqual(sub.status, 'up_to_date')
        self.assertLessEqual(sub.next_due, 1000 + 3600)
        self.assertEqual(sub.data['recent_at'], now - 5 * 3600)

    async def test_failed_retry_does_not_block_due_recent_poll(self):
        sub, now = self.subject(6 * 3600)
        async def fetch(start, end, publish=True):
            sub.calls.append((start, end, publish))
            if start < now - 14 * manager.DAY:
                raise manager.ApiError('historical error')
        sub.fetch_window = fetch
        await sub.run()
        self.assertEqual(sub.calls[0], (now - 14 * manager.DAY, now, True))
        self.assertEqual(sub.data['recent_at'], now)
        self.assertTrue(sub.data['pending'])

    async def test_recent_diagnostics_survive_later_historical_queries(self):
        sub, now = self.subject(6 * 3600)
        point = {'commodity': 'ELECTRICITY', 'intervals': {}, 'dirty': {}}
        sub.data['points']['test'] = point
        async def fetch(start, end, publish=True):
            point['last_query'] = {'start': model.iso(start), 'end': model.iso(end),
                                   'returned_slots': 1, 'empty_slots': 0}
        sub.fetch_window = fetch
        await sub.run()
        self.assertEqual(point['last_recent_query']['end'], model.iso(now))
        self.assertNotEqual(point['last_query']['end'], point['last_recent_query']['end'])
        self.assertIsNotNone(sub.data['last_recent_sync'])

    async def test_historical_only_run_does_not_claim_recent_fetch(self):
        sub, now = self.subject(3600)
        sub.data['last_recent_sync'] = '2026-10-18T00:00:01+00:00'
        await sub.run()
        self.assertEqual(sub.data['last_recent_sync'], '2026-10-18T00:00:01+00:00')

    async def test_forced_sync_waits_for_busy_import(self):
        sub, now = self.subject(3600, pending=False)
        await sub.lock.acquire()
        task = asyncio.create_task(sub.run(force=True))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            sub.lock.release()
            await task
        self.assertEqual(sub.data['recent_at'], now)
