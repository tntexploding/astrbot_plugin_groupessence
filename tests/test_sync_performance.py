from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from group_essence_extractor.astrbot_source import AstrBotEssenceSource
from group_essence_extractor.db import EssenceRepository
from group_essence_extractor.models import EssenceMessage
from group_essence_extractor.plugin_service import GroupEssencePluginService
from group_essence_extractor.sync_timing import measure_sync, stage


class SyncPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_finish_while_sync_waits_on_onebot(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            repository = EssenceRepository(Path(folder) / "essence.db")
            repository.init_db()
            repository.upsert_messages([
                EssenceMessage(source="onebot", message_id="m1", group_id="123456",
                               content_text="cached", sender="sender", sender_time="2026-01-01",
                               essence_time="2026-01-01", operator="operator"),
                EssenceMessage(source="onebot", message_id="m2", group_id="654321",
                               content_text="other group", sender="sender", sender_time="2026-01-01",
                               essence_time="2026-01-01", operator="operator"),
            ])
            entered, release = asyncio.Event(), asyncio.Event()

            class SlowApi:
                async def call_action(self, *, action, **params):
                    entered.set()
                    await release.wait()
                    return []

            logs: list[str] = []
            service = GroupEssencePluginService(AstrBotEssenceSource(), repository,
                                                timing_logger=logs.append)
            sync = asyncio.create_task(service.sync(SlowApi(), "123456"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                recent, search, status = await asyncio.wait_for(asyncio.gather(
                    service.recent("123456", 5), service.search("123456", "cached", 5),
                    service.status()), 2)
                self.assertFalse(sync.done())
                self.assertEqual(recent.total, 1)
                self.assertEqual(search.total, 1)
                self.assertEqual(status.total, 2)
                self.assertTrue(all(row["group_id"] == "123456" for row in recent.items))
            finally:
                release.set()
                await sync
            self.assertEqual(len(logs), 1)
            self.assertIn("outcome=ok", logs[0])
            for field in ("total_ms", "queue_ms", "list_ms", "detail_ms", "normalize_ms",
                          "database_read_ms", "history_ms", "persist_ms"):
                self.assertRegex(logs[0], field + r"=\d+")
            self.assertNotIn("123456", logs[0])
            self.assertNotIn("cached", logs[0])

    async def test_cancelled_sync_records_partial_timing_and_releases_lock(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            entered = asyncio.Event()

            class Api:
                async def call_action(self, *, action, **params):
                    entered.set()
                    await asyncio.Event().wait()

            logs: list[str] = []
            service = GroupEssencePluginService(AstrBotEssenceSource(),
                EssenceRepository(Path(folder) / "not-created.db"), timing_logger=logs.append)
            task = asyncio.create_task(service.sync(Api(), "123456"))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(service.operation_lock.locked())
            self.assertIn("outcome=cancelled", logs[0])
            self.assertNotIn("123456", logs[0])
            self.assertFalse(service.repository.db_path.exists())

    async def test_error_details_never_enter_metrics(self) -> None:
        logs: list[str] = []
        with self.assertRaisesRegex(ValueError, "private payload"):
            with measure_sync(logs.append):
                with stage("list"):
                    raise ValueError("private payload https://private.example/token")
        self.assertEqual(len(logs), 1)
        self.assertIn("outcome=error", logs[0])
        self.assertNotIn("private", logs[0])
        self.assertNotIn("http", logs[0])

    async def test_timing_callback_failure_cannot_fail_sync(self) -> None:
        def broken(message):
            raise RuntimeError("logger failed")
        with measure_sync(broken):
            with stage("list"):
                pass

    async def test_timing_context_is_reset_between_runs(self) -> None:
        logs: list[str] = []
        with measure_sync(logs.append):
            with stage("list"):
                await asyncio.sleep(0.01)
        with measure_sync(logs.append):
            with stage("untrusted_group_123456"):
                pass
        self.assertIn("list_ms=0", logs[1])
        self.assertNotIn("untrusted", logs[1])


if __name__ == "__main__":
    unittest.main()
