"""Verify fast task submission against a real local Git compare-and-swap."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from scripts import green_bridge as bridge
from tests.support_request_api import LocalRequestTransport
from tests.test_green_bridge import git

ROOT = Path(__file__).resolve().parents[1]


class RequestApiSubmitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="request-api-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        seed = self.root / "seed"
        self.control = self.root / "blue.git"
        self.green = self.root / "green.git"
        git("init", "--bare", str(self.remote))
        git("init", str(seed))
        git("config", "user.name", "Test", cwd=seed)
        git("config", "user.email", "test@example.invalid", cwd=seed)
        (seed / "seed.txt").write_text("seed\n", encoding="utf-8")
        git("add", "seed.txt", cwd=seed)
        git("commit", "-m", "seed", cwd=seed)
        git("branch", "pingpong/requests", cwd=seed)
        git("branch", "pingpong/results", cwd=seed)
        git("remote", "add", "origin", str(self.remote), cwd=seed)
        git("push", "origin", "pingpong/requests", "pingpong/results", cwd=seed)
        git("clone", "--bare", str(self.remote), str(self.control))
        git("clone", "--bare", str(self.remote), str(self.green))
        self.config = self.root / "blue.json"
        self.config.write_text(json.dumps({"git_dir": str(self.control), "state_dir": str(self.root / "blue-state"),
                                           "api_submit": True}), encoding="utf-8")
        self.green_config = self.root / "green.json"
        self.green_config.write_text(json.dumps({"git_dir": str(self.green), "state_dir": str(self.root / "green-state"),
                                                 "poll_seconds": 1,
                                                 "adapter_argv": [sys.executable, "-c", "pass"]}), encoding="utf-8")
        self.run_bridge(self.green_config, "serve", "--once")
        settings = bridge.load_settings(self.config)
        transport = bridge.GitTransport(settings)
        self.initial = transport.branch_tip(settings.requests_branch)
        bridge.remember_request_commit(transport, bridge.request_cache_ref(transport, settings), self.initial)

    def run_bridge(self, config: Path, *arguments: str, env: dict = None, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, "-m", "tests.support_request_api", "--config", str(config), *arguments],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=50, check=False)
        if check and result.returncode:
            self.fail("bridge command failed: " + result.stderr)
        return result

    def request(self, command_id: str, script: str = "print('OK')") -> Path:
        path = self.root / (command_id + ".json")
        value = {"version": 1, "id": command_id, "action": "command.execute",
                 "command": {"mode": "argv", "argv": [sys.executable, "-c", script],
                             "cwd": str(self.root), "report_running": False}, "timeout_seconds": 10}
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def submit(self, path: Path, *extra: str, env: dict = None, check: bool = True) -> subprocess.CompletedProcess:
        return self.run_bridge(self.config, "submit", str(path), "--task", "project", "--fast", *extra,
                               env=env, check=check)

    def group(self) -> dict:
        return json.loads(git("--git-dir", str(self.remote), "show",
                              "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))

    def test_ack_loss_recovers_the_existing_request_without_reexecution(self) -> None:
        marker = self.root / "executed"
        script = ("from pathlib import Path; p=Path(" + repr(str(marker)) + "); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X')")
        request = self.request("lost-ack", script)
        lost = self.root / "lose-ack"
        lost.write_bytes(b"1")
        env = os.environ.copy()
        env["PINGPONG_REQUEST_API_LOST_ACK"] = str(lost)
        result = json.loads(self.submit(request, env=env).stdout)
        self.assertFalse(lost.exists())
        self.assertEqual([item["id"] for item in self.group()["commands"]], ["lost-ack"])
        again = json.loads(self.submit(request).stdout)
        self.assertEqual(result["request_sha256"], again["request_sha256"])
        self.run_bridge(self.green_config, "serve", "--once")
        self.run_bridge(self.green_config, "serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")
        history = git("--git-dir", str(self.remote), "log", "--format=%H", "refs/heads/pingpong/requests",
                      "--", "pingpong/task-requests/project.json").splitlines()
        self.assertEqual(len(history), 1)

    def test_two_simultaneous_writers_preserve_both_commands(self) -> None:
        barrier = self.root / "barrier"
        barrier.mkdir()
        env = os.environ.copy()
        env["PINGPONG_REQUEST_API_BARRIER"] = str(barrier)
        paths = [self.request("parallel-" + str(index)) for index in range(2)]
        processes = [subprocess.Popen([sys.executable, "-m", "tests.support_request_api", "--config", str(self.config),
                                      "submit", str(path), "--task", "project", "--fast"], cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for path in paths]
        for process in processes:
            output, error = process.communicate(timeout=50)
            self.assertEqual(process.returncode, 0, error)
            self.assertIn("request_branch_commit", json.loads(output))
        self.assertEqual(len(list(barrier.iterdir())), 2)
        self.assertEqual({item["id"] for item in self.group()["commands"]}, {"parallel-0", "parallel-1"})
        self.run_bridge(self.green_config, "serve", "--once")
        with sqlite3.connect(str(self.root / "green-state" / "state.sqlite3")) as connection:
            count = connection.execute("SELECT count(*) FROM jobs WHERE state = 'completed'").fetchone()[0]
        self.assertEqual(count, 2)

    def test_rewound_branch_refuses_api_and_git_submission(self) -> None:
        self.submit(self.request("prior"))
        git("--git-dir", str(self.remote), "update-ref", "refs/heads/pingpong/requests", self.initial)
        result = self.submit(self.request("after-rewrite"), check=False)
        self.assertNotEqual(result.returncode, 0)
        actual = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests")
        self.assertEqual(actual, self.initial)
        ordinary = self.run_bridge(self.config, "submit", str(self.request("ordinary-after-rewrite")),
                                   "--task", "project", check=False)
        self.assertNotEqual(ordinary.returncode, 0)
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests"), self.initial)

    def test_large_task_uses_git_and_skips_later_api_reads(self) -> None:
        settings = bridge.load_settings(self.config)
        transport = bridge.GitTransport(settings)
        large = json.loads(self.request("old", "x" * 80000).read_text(encoding="utf-8"))
        content = bridge.json_bytes({"version": 2, "task_id": "project", "commands": [large]})
        transport.publish(settings.requests_branch, bridge.GROUP_REQUEST_PREFIX + "project.json", content,
                          base_hint=self.initial, confirm_remote=False)
        reads = self.root / "reads"
        calls = self.root / "calls"
        env = os.environ.copy()
        env.update({"PINGPONG_REQUEST_API_READS": str(reads), "PINGPONG_REQUEST_API_CALLS": str(calls)})
        self.submit(self.request("new-one"), env=env)
        self.submit(self.request("new-two"), env=env)
        self.assertEqual(reads.read_bytes(), b"X")
        self.assertFalse(calls.exists())
        self.assertEqual([item["id"] for item in self.group()["commands"]], ["old", "new-one", "new-two"])

    def test_same_identifier_with_different_content_is_rejected(self) -> None:
        request = self.request("same", "print('one')")
        self.submit(request)
        request = self.request("same", "print('two')")
        self.assertNotEqual(self.submit(request, check=False).returncode, 0)
        self.assertEqual(len(self.group()["commands"]), 1)

    def test_rate_limit_uses_git_for_later_requests_until_retry(self) -> None:
        marker = self.root / "rate-limit"
        reads = self.root / "reads"
        marker.write_bytes(b"1")
        env = os.environ.copy()
        env.update({"PINGPONG_REQUEST_API_RATE_LIMIT": str(marker), "PINGPONG_REQUEST_API_READS": str(reads)})
        self.submit(self.request("limited-one"), env=env)
        self.submit(self.request("limited-two"), env=env)
        self.assertFalse(marker.exists())
        self.assertEqual(reads.read_bytes(), b"X")
        self.assertEqual([item["id"] for item in self.group()["commands"]], ["limited-one", "limited-two"])

    def test_api_unavailable_falls_back_to_git(self) -> None:
        env = os.environ.copy()
        env["PINGPONG_REQUEST_API_UNAVAILABLE"] = "1"
        self.submit(self.request("fallback"), env=env)
        self.assertEqual([item["id"] for item in self.group()["commands"]], ["fallback"])

    def test_api_submit_is_opt_in_and_requires_a_boolean(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.pop("api_submit")
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.assertFalse(bridge.load_settings(self.config).api_submit)
        config["api_submit"] = "yes"
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(bridge.BridgeError):
            bridge.load_settings(self.config)

    def test_fast_batch_appends_all_commands_and_executes_each_once(self) -> None:
        items = [json.loads(self.request("batch-" + str(index)).read_text()) for index in range(3)]
        path = self.root / "batch.json"
        path.write_text(json.dumps({"commands": items}), encoding="utf-8")
        result = self.run_bridge(self.config, "submit-batch", str(path), "--task", "project", "--fast")
        self.assertEqual(len(json.loads(result.stdout)["commands"]), 3)
        self.assertEqual([item["id"] for item in self.group()["commands"]], ["batch-0", "batch-1", "batch-2"])
        self.run_bridge(self.green_config, "serve", "--once")
        self.run_bridge(self.green_config, "serve", "--once")
        with sqlite3.connect(str(self.root / "green-state" / "state.sqlite3")) as connection:
            count = connection.execute("SELECT count(*) FROM jobs WHERE state = 'completed'").fetchone()[0]
        self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()
