"""Check the durable-state gate used by the Windows upgrade watcher."""
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "upgrade_green.py"
SPEC = importlib.util.spec_from_file_location("pingpong_upgrade", SCRIPT)
UPGRADE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPGRADE)


class UpgradeGateTests(unittest.TestCase):
    def test_waits_for_published_completion_and_no_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            database = Path(temporary) / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'running', '')")
            self.assertIsNone(UPGRADE.job_ready(database, "upgrade"))
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE jobs SET state='completed' WHERE id='upgrade'")
            self.assertIsNone(UPGRADE.job_ready(database, "upgrade"))
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE jobs SET published_state='completed' WHERE id='upgrade'")
                connection.execute("INSERT INTO jobs VALUES ('other', 'running', '')")
            self.assertIsNone(UPGRADE.job_ready(database, "upgrade"))
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE jobs SET state='completed' WHERE id='other'")
            self.assertTrue(UPGRADE.job_ready(database, "upgrade"))
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE jobs SET state='unknown' WHERE id='upgrade'")
            self.assertFalse(UPGRADE.job_ready(database, "upgrade"))

    def test_source_comparison_accepts_windows_line_endings_and_bom(self) -> None:
        self.assertEqual(UPGRADE.normalized_hash(b"a\nb\n"), UPGRADE.normalized_hash(b"\xef\xbb\xbfa\r\nb\r\n"))

    def test_stage_verifies_baseline_before_replacing_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            service = scripts / "green_bridge.py"
            service.write_bytes(b"print('old')\r\n")
            helper = SCRIPT.read_bytes()
            config = {"state_dir": str(root / "state"), "git_dir": str(root / "git"),
                      "adapter_argv": ["python", str(scripts / "codeagent_adapter.py")]}
            base = "a" * 40
            new = "b" * 40
            def git_output(_config: dict, args: list) -> bytes:
                if args[0] == "fetch":
                    return b""
                if args[1] == base + ":scripts/green_bridge.py":
                    return b"print('old')\n"
                if args[1] == new + ":scripts/green_bridge.py":
                    return b"print('new')\n"
                return helper
            def register(_config: dict, _script: str) -> None:
                self.assertEqual(service.read_bytes(), b"print('old')\r\n")
            with mock.patch.object(UPGRADE.sys, "platform", "win32"), \
                    mock.patch.object(UPGRADE, "git_output", side_effect=git_output), \
                    mock.patch.object(UPGRADE, "powershell", side_effect=register), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                UPGRADE.stage(config, root / "config.json", base, new, "upgrade")
            self.assertEqual(service.read_bytes(), b"print('new')\n")
            marker = json.loads((root / "state" / "upgrades" / "upgrade" / "ready.json").read_text())
            self.assertEqual(marker["commit"], new)
            self.assertEqual(marker["sha256"], UPGRADE.normalized_hash(service.read_bytes()))
            previous = (root / "state" / "upgrades" / "upgrade" / "previous_service.py").read_bytes()
            self.assertEqual(previous, b"print('old')\r\n")
            self.assertEqual(marker["previous_sha256"], UPGRADE.normalized_hash(previous))

    def test_changed_baseline_is_not_overwritten_or_registered(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            service = scripts / "green_bridge.py"
            service.write_bytes(b"user change\n")
            config = {"state_dir": str(root / "state"), "git_dir": str(root / "git"),
                      "adapter_argv": ["python", str(scripts / "codeagent_adapter.py")]}
            with mock.patch.object(UPGRADE.sys, "platform", "win32"), \
                    mock.patch.object(UPGRADE, "git_output", return_value=b"baseline\n"), \
                    mock.patch.object(UPGRADE, "powershell") as scheduler:
                with self.assertRaises(UPGRADE.UpgradeError):
                    UPGRADE.stage(config, root / "config.json", "a" * 40, "b" * 40, "upgrade")
            scheduler.assert_not_called()
            self.assertEqual(service.read_bytes(), b"user change\n")

    def test_watcher_does_not_restart_failed_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            state = root / "state"
            directory = state / "upgrades" / "upgrade"
            directory.mkdir(parents=True)
            database = state / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'unknown', 'unknown')")
            config = {"state_dir": str(state), "adapter_argv": ["python", str(root / "codeagent_adapter.py")]}
            with mock.patch.object(UPGRADE, "powershell") as scheduler:
                self.assertFalse(UPGRADE.watch(config, root / "config.json", "b" * 40, "upgrade"))
            self.assertEqual(scheduler.call_count, 1)
            self.assertIn("Unregister-ScheduledTask", scheduler.call_args.args[1])

    def test_watcher_restarts_only_after_verified_published_job(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            state = root / "state"
            directory = state / "upgrades" / "upgrade"
            directory.mkdir(parents=True)
            service = root / "green_bridge.py"
            service.write_bytes(b"verified new source\n")
            commit = "b" * 40
            (directory / "ready.json").write_text(json.dumps({"commit": commit,
                                                               "sha256": UPGRADE.normalized_hash(service.read_bytes())}))
            with sqlite3.connect(state / "state.sqlite3") as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'completed', 'completed')")
            config = {"state_dir": str(state), "adapter_argv": ["python", str(root / "codeagent_adapter.py")]}
            with mock.patch.object(UPGRADE, "powershell") as scheduler:
                self.assertTrue(UPGRADE.watch(config, root / "config.json", commit, "upgrade"))
            self.assertEqual(scheduler.call_count, 4)
            self.assertIn("Stop-ScheduledTask", scheduler.call_args_list[0].args[1])
            self.assertIn("Start-ScheduledTask", scheduler.call_args_list[1].args[1])
            self.assertEqual((directory / "watch.log").read_text(), "SERVICE_RESTARTED\n")

    def test_quiescent_restart_holds_database_lock(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            database = Path(temporary) / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'completed', 'completed')")

            def check_lock() -> None:
                with sqlite3.connect(database, timeout=0) as other:
                    with self.assertRaises(sqlite3.OperationalError):
                        other.execute("INSERT INTO jobs VALUES ('new', 'queued', '')")

            self.assertTrue(UPGRADE.when_quiescent(database, "upgrade", check_lock))
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO jobs VALUES ('new', 'queued', '')")
            action = mock.Mock()
            self.assertFalse(UPGRADE.when_quiescent(database, "upgrade", action))
            action.assert_not_called()

    def test_failed_restart_restores_previous_service(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            state = root / "state"
            directory = state / "upgrades" / "upgrade"
            directory.mkdir(parents=True)
            service = root / "green_bridge.py"
            previous = b"previous source\n"
            service.write_bytes(b"new source\n")
            (directory / "previous_service.py").write_bytes(previous)
            commit = "b" * 40
            (directory / "ready.json").write_text(json.dumps({
                "commit": commit, "sha256": UPGRADE.normalized_hash(service.read_bytes()),
                "previous_sha256": UPGRADE.normalized_hash(previous),
            }))
            with sqlite3.connect(state / "state.sqlite3") as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'completed', 'completed')")
            config = {"state_dir": str(state), "adapter_argv": ["python", str(root / "codeagent_adapter.py")]}
            outcomes = [None, UPGRADE.UpgradeError("start failed"), None, None, None, None]
            with mock.patch.object(UPGRADE, "powershell", side_effect=outcomes) as scheduler:
                self.assertFalse(UPGRADE.watch(config, root / "config.json", commit, "upgrade"))
            self.assertEqual(service.read_bytes(), previous)
            self.assertIn("PREVIOUS_SERVICE_RESTORED", (directory / "watch.log").read_text())
            self.assertEqual(scheduler.call_count, 6)

    def test_failed_restart_does_not_rollback_active_job(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pingpong-upgrade-test-") as temporary:
            root = Path(temporary)
            state = root / "state"
            directory = state / "upgrades" / "upgrade"
            directory.mkdir(parents=True)
            service = root / "green_bridge.py"
            previous = b"previous source\n"
            service.write_bytes(b"new source\n")
            (directory / "previous_service.py").write_bytes(previous)
            commit = "b" * 40
            (directory / "ready.json").write_text(json.dumps({
                "commit": commit, "sha256": UPGRADE.normalized_hash(service.read_bytes()),
                "previous_sha256": UPGRADE.normalized_hash(previous),
            }))
            database = state / "state.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE jobs (id TEXT, state TEXT, published_state TEXT)")
                connection.execute("INSERT INTO jobs VALUES ('upgrade', 'completed', 'completed')")
            config = {"state_dir": str(state), "adapter_argv": ["python", str(root / "codeagent_adapter.py")]}
            count = 0

            def scheduler(_config: dict, _script: str) -> None:
                nonlocal count
                count += 1
                if count == 2:
                    with sqlite3.connect(database) as connection:
                        connection.execute("INSERT INTO jobs VALUES ('other', 'running', '')")
                    raise UPGRADE.UpgradeError("start failed")

            with mock.patch.object(UPGRADE, "powershell", side_effect=scheduler):
                self.assertFalse(UPGRADE.watch(config, root / "config.json", commit, "upgrade"))
            self.assertEqual(service.read_bytes(), b"new source\n")
            self.assertIn("Automatic rollback was not completed", (directory / "watch.log").read_text())


if __name__ == "__main__":
    unittest.main()
