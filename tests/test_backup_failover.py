"""Server backup reported as paused while homelab-backup's failover flag exists."""

import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from homelab_monitor.alerts import backup_alert_hint
from homelab_monitor.backup_failover import mark_paused_by_failover, read_failover_since
from homelab_monitor.formatting import RED, backup_line
from homelab_monitor.metrics import server_backup_status

FLAG_LINE = "2026-01-02 00:44:42 data drive MISSING"
BACKUP_CFG = SimpleNamespace(backup_max_age_hours=48)


class BackupPausedByFailoverTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.flag = os.path.join(self.folder.name, "failover.active")
        self.log = os.path.join(self.folder.name, "backup.log")

    def write_flag(self, text=FLAG_LINE + "\n"):
        with open(self.flag, "w", encoding="utf-8") as handle:
            handle.write(text)

    def write_log_copied_mid_run(self):
        """The backup drive's copy of the log: last run started, never finished."""
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write("2026-01-01 04:00:00 === Backup started ===\n")
        day_ago = time.time() - 24 * 3600
        os.utime(self.log, (day_ago, day_ago))

    def test_no_flag_leaves_the_stage_alone(self):
        self.write_log_copied_mid_run()
        stage = server_backup_status(self.log)
        self.assertIs(mark_paused_by_failover(stage, self.flag), stage)
        self.assertIs(mark_paused_by_failover(stage, ""), stage)
        self.assertIsNone(read_failover_since(self.flag))

    def test_flag_turns_never_finished_into_paused_with_the_reason(self):
        self.write_log_copied_mid_run()
        self.write_flag()
        before = server_backup_status(self.log)
        self.assertIn("never finished", before["detail"])  # the 2026-11-12 alert
        stage = mark_paused_by_failover(before, self.flag)
        icon, text = backup_line(stage, BACKUP_CFG)
        self.assertEqual(icon, RED)
        self.assertEqual(text, f"PAUSED — failover active since {FLAG_LINE}")
        self.assertNotIn("never finished", text)

    def test_flag_wins_when_the_log_is_missing(self):
        self.write_flag()
        stage = mark_paused_by_failover(server_backup_status(self.log), self.flag)
        self.assertTrue(stage["available"])
        self.assertIn("PAUSED", backup_line(stage, BACKUP_CFG)[1])

    def test_empty_flag_file_still_counts(self):
        self.write_flag("")
        self.assertEqual(read_failover_since(self.flag), "an unknown time")

    def test_hint_points_at_failback_not_the_scheduler(self):
        paused = backup_alert_hint("stage1", {"status": "paused"})
        self.assertIn("failback", paused)
        self.assertNotIn("crontab", paused)
        self.assertIn("crontab", backup_alert_hint("stage1", {"status": "failed"}))
        self.assertEqual(backup_alert_hint("stage2", {"status": "failed"}), "")


if __name__ == "__main__":
    unittest.main()
