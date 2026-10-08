import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "collector", Path(__file__).resolve().parents[1] / "collect_codex.py"
)
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


class KCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.kcode = self.root / "kcode.sqlite"
        self.zcode = self.root / "zcode.sqlite"
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(collector, "AGENTBOARD_DIR", str(self.root)),
            patch.object(collector, "ZCODE_DB_CANDIDATES", (str(self.kcode), str(self.zcode))),
            patch.object(collector, "ZCODE_DB_DEFAULT", str(self.kcode)),
        ):
            context.start()
            self.addCleanup(context.stop)

    def config(self, **values):
        (self.root / "config.json").write_text(json.dumps(values), encoding="utf-8")

    def test_kcode_wins_even_when_zcode_and_sidecars_are_newer(self):
        self.kcode.touch()
        self.zcode.touch()
        os.utime(self.kcode, (1, 1))
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.zcode) + suffix).touch()
        self.assertEqual(collector.zcode_db_path(), str(self.kcode))

    def test_zcode_fallback_requires_main_database(self):
        Path(str(self.kcode) + "-shm").touch()
        self.zcode.touch()
        self.assertEqual(collector.zcode_db_path(), str(self.zcode))

    def test_kcode_only(self):
        self.kcode.touch()
        self.assertEqual(collector.zcode_db_path(), str(self.kcode))

    def test_missing_databases_return_kcode_default(self):
        Path(str(self.zcode) + "-wal").touch()
        self.assertEqual(collector.zcode_db_path(), str(self.kcode))

    def test_kcode_environment_overrides_legacy_and_config(self):
        os.environ["AGENTBOARD_KCODE_DB"] = "  ~/custom-kcode.sqlite  "
        os.environ["AGENTBOARD_ZCODE_DB"] = "legacy.sqlite"
        self.config(kcode_db_path="configured.sqlite")
        self.assertEqual(collector.zcode_db_path(), os.path.expanduser("~/custom-kcode.sqlite"))

    def test_legacy_environment_keeps_precedence_over_config(self):
        os.environ["AGENTBOARD_ZCODE_DB"] = "legacy.sqlite"
        self.config(kcode_db_path="configured.sqlite")
        self.assertEqual(collector.zcode_db_path(), "legacy.sqlite")

    def test_kcode_config_overrides_legacy_config(self):
        self.config(kcode_db_path=" new.sqlite ", zcode_db_path="old.sqlite")
        self.assertEqual(collector.zcode_db_path(), "new.sqlite")

    def test_legacy_config_remains_supported(self):
        self.config(zcode_db_path="old.sqlite")
        self.assertEqual(collector.zcode_db_path(), "old.sqlite")

    def test_blank_overrides_use_detection(self):
        os.environ["AGENTBOARD_KCODE_DB"] = " "
        self.config(kcode_db_path=" ", zcode_db_path=" ")
        self.zcode.touch()
        self.assertEqual(collector.zcode_db_path(), str(self.zcode))

    def create_usage_database(self):
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.kcode)) as connection, connection:
            connection.executescript("""
                CREATE TABLE session (id TEXT, directory TEXT, time_updated INTEGER);
                CREATE TABLE model_usage (
                    session_id TEXT, logical_request_id TEXT, status TEXT,
                    started_at INTEGER, completed_at INTEGER, input_tokens INTEGER,
                    output_tokens INTEGER, reasoning_tokens INTEGER,
                    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
                    computed_total_tokens INTEGER
                );
                CREATE TABLE message (
                    session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
                );
                CREATE TABLE tool_usage (
                    session_id TEXT, started_at INTEGER, completed_at INTEGER, tool_name TEXT
                );
            """)
            connection.execute("INSERT INTO session VALUES ('shared', '', ?)", (now,))
            connection.execute(
                "INSERT INTO model_usage VALUES ('shared', 'request', 'completed', ?, ?, 100, 20, 5, 3, 50, 120)",
                (now, now + 1000),
            )
            connection.execute(
                "INSERT INTO model_usage VALUES ('shared', 'failed', 'error', ?, ?, 900, 90, 0, 0, 0, 990)",
                (now, now + 1000),
            )
            connection.execute(
                "INSERT INTO message VALUES ('shared', ?, ?, ?)",
                (now, now, '{"role":"user","content":"private"}'),
            )
            connection.execute("INSERT INTO tool_usage VALUES ('shared', ?, ?, 'read')", (now, now))

    def test_kcode_retains_zcode_identity_and_token_policy(self):
        self.create_usage_database()
        sessions, daily, meta = collector.zcode_collect_sessions()
        self.assertEqual(meta["db_path"], str(self.kcode))
        self.assertEqual(len(sessions), 1)
        entry = sessions[0]
        self.assertEqual(entry["session_id"], "opencode:zcode:shared")
        self.assertEqual(entry["tokens_used"], 120)
        self.assertEqual(entry["input_tokens"], 100)
        self.assertEqual(entry["cache_read_tokens"], 50)
        self.assertEqual(entry["messages"], 1)
        self.assertEqual(entry["tool_calls"], 1)
        self.assertNotIn("private", json.dumps(sessions))
        self.assertEqual(daily[entry["date"]]["tokens_used"], 120)

    def test_sync_uses_opencode_and_skips_unchanged_second_run(self):
        self.create_usage_database()
        with patch.object(collector, "post_session") as post:
            first = collector.sync_zcode({})
            second = collector.sync_zcode({})
        self.assertEqual(first["synced"], 1)
        self.assertEqual(first["errors"], 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["source"], "opencode")
        self.assertEqual(second["synced"], 0)
        self.assertTrue(second["unchanged"])

    def test_windowless_logging_does_not_require_stderr(self):
        log = self.root / "sync.log"
        with patch.object(collector, "LOG_DIR", str(self.root)), patch.object(
            collector, "SYNC_LOG_PATH", str(log)
        ), patch.object(collector.sys, "stderr", None):
            collector.log_sync("windowless sync")
        self.assertIn("windowless sync", log.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
