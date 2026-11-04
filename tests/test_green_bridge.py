"""Behavioral tests against local Git repositories and a fake agent adapter."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "green_bridge.py"
FAKE_ADAPTER = r'''import json
import os
import sys
import time
from pathlib import Path
request = json.load(sys.stdin)
calls = Path(sys.argv[1])
with calls.open("a", encoding="utf-8") as stream:
    stream.write(request["id"] + "\n")
with calls.with_suffix(".inputs").open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(request) + "\n")
with calls.with_suffix(".gitconfig").open("a", encoding="utf-8") as stream:
    stream.write(os.environ.get("GIT_CONFIG_GLOBAL", "") + "\n")
if request["message"] == "sleep":
    time.sleep(20)
if request["message"] == "reject-result":
    hook = Path(sys.argv[2]) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
native = request["native_session_id"] or "mock-" + request["session"]
response = {"version": 1, "status": "completed", "native_session_id": native,
            "summary": "mock response", "command_evidence": [{"exit_code": 0}],
            "context": {"compaction_detected": request["message"] == "compact",
                        "used_tokens": 72, "limit_tokens": 100},
            "checkpoint": "mock checkpoint"}
json.dump(response, sys.stdout)
'''


def git(*args: str, cwd: Path = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


class GreenBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="pingpong-test-")
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.control = self.root / "control.git"
        self.state = self.root / "state"
        self.calls = self.root / "calls.txt"
        self.fake = self.root / "fake_adapter.py"
        self.config = self.root / "config.json"
        git("init", "--bare", str(self.remote))
        git("init", str(self.seed))
        git("config", "user.name", "Test User", cwd=self.seed)
        git("config", "user.email", "test@example.invalid", cwd=self.seed)
        (self.seed / "seed.txt").write_text("seed\n", encoding="utf-8")
        git("add", "seed.txt", cwd=self.seed)
        git("commit", "-m", "seed", cwd=self.seed)
        git("branch", "pingpong/requests", cwd=self.seed)
        git("branch", "pingpong/results", cwd=self.seed)
        git("remote", "add", "origin", str(self.remote), cwd=self.seed)
        git("push", "origin", "pingpong/requests", "pingpong/results", cwd=self.seed)
        git("clone", "--bare", str(self.remote), str(self.control))
        self.fake.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.write_config()
        self.call("serve", "--once")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_config(self, **updates: object) -> None:
        config = {"git_dir": str(self.control), "state_dir": str(self.state), "remote": "origin",
                  "requests_branch": "pingpong/requests", "results_branch": "pingpong/results",
                  "adapter_argv": [sys.executable, str(self.fake), str(self.calls), str(self.remote)],
                  "poll_seconds": 1, "default_timeout_seconds": 5, "max_timeout_seconds": 30,
                  "start_after": "HEAD"}
        config.update(updates)
        self.config.write_text(json.dumps(config), encoding="utf-8")

    def call(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(BRIDGE), "--config", str(self.config), *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=40)
        if check and result.returncode:
            self.fail("bridge failed: " + result.stderr)
        return result

    def submit(self, task_id: str, action: str, session: str = None,
               message: str = "", **extra: object) -> dict:
        request = {"version": 1, "id": task_id, "action": action, "message": message}
        if session is not None:
            request["session"] = session
        request.update(extra)
        path = self.root / (task_id + ".json")
        path.write_text(json.dumps(request), encoding="utf-8")
        return json.loads(self.call("submit", str(path)).stdout)

    def result(self, task_id: str) -> dict:
        return json.loads(self.call("status", task_id).stdout)

    def calls_list(self) -> list:
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_new_switch_execute_and_duplicate_submission(self) -> None:
        first = self.submit("job-1", "session.new", "project")
        self.call("serve", "--once")
        self.assertEqual(self.result("job-1")["state"], "completed")
        self.assertTrue(self.result("job-1")["agent_report"]["native_session_id"])
        repeated = self.submit("job-1", "session.new", "project")
        self.assertEqual(first["request_sha256"], repeated["request_sha256"])
        self.submit("job-2", "session.switch", "project")
        self.submit("job-3", "task.execute", message="compact")
        self.call("serve", "--once")
        result = self.result("job-3")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["session"], "project")
        self.assertTrue(result["agent_report"]["context"]["compaction_detected"])
        self.call("serve", "--once")
        self.assertEqual(self.calls_list(), ["job-1", "job-2", "job-3"])
        path = self.root / "collision.json"
        path.write_text(json.dumps({"version": 1, "id": "job-1", "action": "session.new",
                                    "session": "different"}), encoding="utf-8")
        self.assertNotEqual(self.call("submit", str(path), check=False).returncode, 0)

    def test_checkpoint_and_attach_existing_session(self) -> None:
        self.submit("attach", "session.attach", "older", native_session_id="native-42")
        self.call("serve", "--once")
        self.assertEqual(self.result("attach")["agent_report"]["native_session_id"], "native-42")
        self.submit("checkpoint", "session.checkpoint", "older", "summarize")
        self.call("serve", "--once")
        self.assertEqual(self.result("checkpoint")["agent_report"]["checkpoint"], "mock checkpoint")
        self.submit("after", "task.execute", "older", "continue",
                    context={"code_commit": "fixed-baseline"})
        self.call("serve", "--once")
        inputs = [json.loads(line) for line in self.calls.with_suffix(".inputs").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(inputs[-1]["previous_checkpoint"], "mock checkpoint")
        self.assertEqual(inputs[-1]["context"]["code_commit"], "fixed-baseline")

    def test_missing_session_fails_without_starting_agent(self) -> None:
        self.submit("missing", "task.execute", "unknown", "do one thing")
        self.call("serve", "--once")
        self.assertEqual(self.result("missing")["state"], "failed")
        self.assertEqual(self.calls_list(), [])

    def test_service_without_adapter_executes_commands_and_rejects_agent_actions(self) -> None:
        self.write_config(adapter_argv=[], state_dir=str(self.root / "direct-only-state"))
        self.call("serve", "--once")
        command = {"mode": "argv", "argv": [sys.executable, "-c", "print('PINGPONG_OK')"],
                   "cwd": str(self.root), "report_running": False}
        self.submit("direct-only", "command.execute", command=command)
        self.call("serve", "--once")
        result = self.result("direct-only")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["command_result"]["exit_code"], 0)
        output = self.root / "direct-only.txt"
        self.call("output", "direct-only", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), b"PINGPONG_OK\n")

        self.submit("no-adapter", "session.new", "project")
        self.call("serve", "--once")
        result = self.result("no-adapter")
        self.assertEqual(result["state"], "failed")
        self.assertIn("adapter_argv", result["message"])
        self.assertEqual(self.calls_list(), [])

    def test_command_returns_exact_binary_output_and_does_not_use_agent(self) -> None:
        marker = self.root / "direct-calls"
        source = ("import os,sys; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1, b'Z'*(4*1024*1024+13)+bytes([0,255])); "
                  "os.write(2, bytes([255,0,13,10])); sys.exit(7)")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)], "cwd": str(self.root)}
        self.submit("direct", "command.execute", "conversation", command=command)
        self.call("serve", "--once")
        result = self.result("direct")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["session"], "conversation")
        self.assertEqual(result["command_result"]["exit_code"], 7)
        self.assertEqual(len(result["command_result"]["stdout"]["parts"]), 2)
        self.assertTrue(result["command_result"]["output_complete"])
        output = self.root / "stdout.bin"
        errors = self.root / "stderr.bin"
        self.call("output", "direct", "--stream", "stdout", "--output", str(output))
        self.call("output", "direct", "--stream", "stderr", "--output", str(errors))
        self.assertEqual(output.read_bytes(), b"Z" * (4 * 1024 * 1024 + 13) + bytes([0, 255]))
        self.assertEqual(errors.read_bytes(), bytes([255, 0, 13, 10]))
        self.assertNotEqual(self.call("output", "direct", "--stream", "stdout", "--output", str(output),
                                      check=False).returncode, 0)
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.calls_list(), [])

    def test_command_timeout_preserves_partial_output_without_retry(self) -> None:
        marker = self.root / "timeout-calls"
        source = ("import os,sys,time; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1,b'partial'); time.sleep(20)")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)], "cwd": str(self.root)}
        self.submit("direct-timeout", "command.execute", command=command, timeout_seconds=1)
        self.call("serve", "--once")
        result = self.result("direct-timeout")
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(result["command_result"]["timed_out"])
        self.assertFalse(result["command_result"]["output_complete"])
        output = self.root / "partial.bin"
        self.call("output", "direct-timeout", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), b"partial")
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.calls_list(), [])

    def test_command_output_uses_fetched_commit_without_remote_access(self) -> None:
        command = {"mode": "argv", "argv": [sys.executable, "-c", "import os; os.write(1,b'cached')"],
                   "cwd": str(self.root), "report_running": False}
        self.submit("direct-cached", "command.execute", command=command)
        self.call("serve", "--once")
        result = self.result("direct-cached")
        history = git("--git-dir", str(self.control), "log", "--format=%H", result["results_branch_commit"],
                      "--", "pingpong/results/direct-cached.json").splitlines()
        self.assertEqual(len(history), 1)
        git("--git-dir", str(self.control), "remote", "set-url", "origin", str(self.root / "missing.git"))
        output = self.root / "cached.bin"
        self.call("output", "direct-cached", "--stream", "stdout", "--output", str(output),
                  "--results-commit", result["results_branch_commit"])
        self.assertEqual(output.read_bytes(), b"cached")

    def test_credential_optimization_does_not_change_dispatched_command_git_config(self) -> None:
        git_config = self.root / "user-gitconfig"
        git_config.write_text("[credential]\n\thelper = manager\n", encoding="utf-8")
        self.write_config(git_config_global=str(git_config), gcm_skip_store=True)
        source = ("import subprocess,sys; "
                  "p=subprocess.run(['git','config','--get-all','credential.helper'],capture_output=True); "
                  "sys.stdout.buffer.write(p.stdout); sys.exit(p.returncode)")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source], "cwd": str(self.root),
                   "report_running": False}
        self.submit("credential-scope", "command.execute", command=command)
        self.call("serve", "--once")
        result = self.result("credential-scope")
        self.assertEqual(result["state"], "completed")
        output = self.root / "credential-scope.out"
        self.call("output", "credential-scope", "--stream", "stdout", "--output", str(output))
        self.assertIn(b"manager\n", output.read_bytes())
        self.assertNotIn(b"f()", output.read_bytes())

    def test_push_ack_publishes_without_confirmation_fetch(self) -> None:
        self.write_config(result_confirmation="push_ack")
        marker = self.root / "ack-marker"
        source = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X')")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                   "cwd": str(self.root), "report_running": False}
        self.submit("ack-publish", "command.execute", command=command)
        settings = bridge.load_settings(self.config)
        transport = bridge.GitTransport(settings)
        connection = bridge.connect_database(self.state / "state.sqlite3")
        try:
            with mock.patch.object(transport, "branch_tip", side_effect=AssertionError("unexpected confirmation fetch")):
                bridge.cycle(connection, transport, settings)
        finally:
            connection.close()
        self.assertEqual(self.result("ack-publish")["state"], "completed")
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")

    def test_lost_push_ack_retries_delivery_without_reexecution(self) -> None:
        self.write_config(result_confirmation="push_ack")
        marker = self.root / "lost-ack-marker"
        source = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X')")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                   "cwd": str(self.root), "report_running": False}
        self.submit("lost-ack", "command.execute", command=command)
        settings = bridge.load_settings(self.config)
        transport = bridge.GitTransport(settings)
        connection = bridge.connect_database(self.state / "state.sqlite3")
        original_run = bridge.GitTransport.run
        lost = False

        def run_with_lost_ack(instance, arguments, data=None, env=None, check=True):
            nonlocal lost
            output = original_run(instance, arguments, data=data, env=env, check=check)
            if arguments[0] == "push" and arguments[-1].endswith(":refs/heads/pingpong/results") and not lost:
                lost = True
                raise bridge.BridgeError("simulated lost push acknowledgement")
            return output

        try:
            with mock.patch.object(bridge.GitTransport, "run", run_with_lost_ack):
                with self.assertRaises(bridge.BridgeError):
                    bridge.cycle(connection, transport, settings)
            row = connection.execute("SELECT state, published_state FROM jobs WHERE id='lost-ack'").fetchone()
            self.assertEqual(tuple(row), ("completed", ""))
            bridge.cycle(connection, transport, settings)
        finally:
            connection.close()
        self.assertTrue(lost)
        self.assertEqual(marker.read_bytes(), b"X")
        result = self.result("lost-ack")
        self.assertEqual(result["state"], "completed")
        history = git("--git-dir", str(self.control), "log", "--format=%H", result["results_branch_commit"],
                      "--", "pingpong/results/lost-ack.json").splitlines()
        self.assertEqual(len(history), 1)

    def test_fast_submit_keeps_immutable_request_and_deduplication(self) -> None:
        marker = self.root / "fast-calls"
        source = ("import os,sys; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1,b'fast')")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                   "cwd": str(self.root), "report_running": False}
        request = {"version": 1, "id": "fast-submit", "action": "command.execute", "command": command}
        path = self.root / "fast-submit.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        first = json.loads(self.call("submit", str(path), "--fast").stdout)
        again = json.loads(self.call("submit", str(path), "--fast").stdout)
        self.assertEqual(first["request_sha256"], again["request_sha256"])
        self.call("serve", "--once")
        self.assertEqual(self.result("fast-submit")["state"], "completed")
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.calls_list(), [])
        request["command"]["report_running"] = True
        path.write_text(json.dumps(request), encoding="utf-8")
        self.assertNotEqual(self.call("submit", str(path), "--fast", check=False).returncode, 0)

    def run_watch_case(self, fail_watch: bool, slow_hint: bool = False) -> None:
        self.write_config(watch_mode="github_api", poll_seconds=1, reconcile_seconds=5 if slow_hint else 60)
        settings = bridge.load_settings(self.config)
        ready = threading.Event()
        stop = threading.Event()
        remote = self.remote
        original_cycle = bridge.cycle

        class LocalWatcher:
            poll_interval = 0
            next_check_at = 0.0

            def __init__(self, transport, current_settings) -> None:
                pass

            def check(self) -> str:
                if stop.is_set():
                    raise KeyboardInterrupt()
                ready.set()
                if fail_watch:
                    raise bridge.BridgeError("simulated API outage")
                if slow_hint:
                    self.next_check_at = time.monotonic() + 300
                return git("--git-dir", str(remote), "rev-parse", "refs/heads/pingpong/requests")

            def close(self) -> None:
                pass

        def run_cycle(connection, transport, current_settings) -> None:
            if stop.is_set():
                raise KeyboardInterrupt()
            original_cycle(connection, transport, current_settings)

        def run_service() -> None:
            try:
                bridge.serve(settings, False)
            except KeyboardInterrupt:
                return  # The test requested a clean worker shutdown.

        marker = self.root / ("fallback-marker" if fail_watch else "watch-marker")
        task_id = "watch-fallback" if fail_watch else "watch-change"
        source = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X')")
        request = {"version": 1, "id": task_id, "action": "command.execute",
                   "command": {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                               "cwd": str(self.root), "report_running": False}}
        with mock.patch.object(bridge, "GitHubRefWatcher", LocalWatcher), mock.patch.object(bridge, "cycle", run_cycle):
            worker = threading.Thread(target=run_service, daemon=True)
            worker.start()
            try:
                self.assertTrue(ready.wait(10), "watcher did not start")
                git("checkout", "pingpong/requests", cwd=self.seed)
                request_dir = self.seed / "pingpong" / "requests"
                request_dir.mkdir(parents=True, exist_ok=True)
                (request_dir / (task_id + ".json")).write_text(json.dumps(request), encoding="utf-8")
                git("add", "pingpong/requests/" + task_id + ".json", cwd=self.seed)
                git("commit", "-m", "add watched request", cwd=self.seed)
                git("push", "origin", "pingpong/requests", cwd=self.seed)
                deadline = time.monotonic() + 20
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "watched request was not executed")
            finally:
                stop.set()
                worker.join(15)
            self.assertFalse(worker.is_alive(), "service did not stop")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.result(task_id)["state"], "completed")
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")

    def test_fast_watch_executes_new_request_once(self) -> None:
        self.run_watch_case(False)

    def test_fast_watch_falls_back_to_git_after_api_failure(self) -> None:
        self.run_watch_case(True)

    def test_fast_watch_reconciles_while_api_poll_is_deferred(self) -> None:
        self.run_watch_case(False, slow_hint=True)

    def test_fast_watch_retries_unpublished_result_without_reexecution(self) -> None:
        self.write_config(watch_mode="github_api", poll_seconds=1, reconcile_seconds=60)
        settings = bridge.load_settings(self.config)
        marker = self.root / "watch-publish-marker"
        source = ("from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X')")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                   "cwd": str(self.root), "report_running": False}
        self.submit("watch-publish", "command.execute", command=command)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        stop = threading.Event()
        original_cycle = bridge.cycle
        remote = self.remote

        class LocalWatcher:
            poll_interval = 0
            next_check_at = 0.0

            def __init__(self, transport, current_settings) -> None:
                pass

            def check(self) -> str:
                if stop.is_set():
                    raise KeyboardInterrupt()
                return git("--git-dir", str(remote), "rev-parse", "refs/heads/pingpong/requests")

            def close(self) -> None:
                pass

        def run_cycle(connection, transport, current_settings) -> None:
            if stop.is_set():
                raise KeyboardInterrupt()
            original_cycle(connection, transport, current_settings)

        def run_service() -> None:
            try:
                bridge.serve(settings, False)
            except KeyboardInterrupt:
                return  # The test requested a clean worker shutdown.

        with mock.patch.object(bridge, "GitHubRefWatcher", LocalWatcher), mock.patch.object(bridge, "cycle", run_cycle):
            worker = threading.Thread(target=run_service, daemon=True)
            worker.start()
            try:
                deadline = time.monotonic() + 20
                log = self.state / "service.log"
                while time.monotonic() < deadline:
                    if marker.exists() and log.exists() and "Cycle failed" in log.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.05)
                self.assertTrue(marker.exists(), "command did not execute")
                self.assertIn("Cycle failed", log.read_text(encoding="utf-8"))
                hook.unlink()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    result_exists = subprocess.run(["git", "--git-dir", str(self.remote), "cat-file", "-e",
                                                    "refs/heads/pingpong/results:pingpong/results/watch-publish.json"],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                    if result_exists.returncode == 0:
                        break
                    time.sleep(0.1)
                self.assertEqual(result_exists.returncode, 0, "result was not republished")
            finally:
                stop.set()
                worker.join(15)
            self.assertFalse(worker.is_alive(), "service did not stop")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.result("watch-publish")["state"], "completed")

    @unittest.skipIf(os.name == "nt", "test process-group shutdown uses POSIX signals")
    def test_interrupted_command_is_unknown_and_not_reexecuted(self) -> None:
        marker = self.root / "crash-calls"
        source = ("import os,sys,time; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1,b'partial'); time.sleep(20)")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker)],
                   "cwd": str(self.root), "report_running": False}
        self.submit("direct-crash", "command.execute", command=command, timeout_seconds=30)
        process = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(self.config), "serve", "--once"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        self.call("serve", "--once")
        self.assertEqual(self.result("direct-crash")["state"], "unknown")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.calls_list(), [])

    @unittest.skipIf(os.name == "nt", "fake PowerShell executable uses a POSIX shebang")
    def test_powershell_script_is_passed_as_utf16_encoded_command(self) -> None:
        executable = self.root / "fake-powershell"
        executable.write_text("#!/usr/bin/env python3\nimport base64,os,sys\n"
                              "args=sys.argv[1:]\n"
                              "text=base64.b64decode(args[args.index('-EncodedCommand')+1]).decode('utf-16le')\n"
                              "os.write(1,text.encode('utf-8'))\n", encoding="utf-8")
        executable.chmod(0o755)
        self.write_config(powershell_executable=str(executable))
        script = "Write-Output '你好'"
        command = {"mode": "powershell", "script": script, "cwd": str(self.root)}
        self.submit("direct-powershell", "command.execute", command=command)
        self.call("serve", "--once")
        self.assertEqual(self.result("direct-powershell")["state"], "completed")
        output = self.root / "powershell.bin"
        self.call("output", "direct-powershell", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), script.encode("utf-8"))
        self.assertEqual(self.calls_list(), [])

    @unittest.skipIf(os.name == "nt", "fake PowerShell executable uses a POSIX shebang")
    def test_long_powershell_script_is_passed_in_a_utf8_bom_file(self) -> None:
        executable = self.root / "fake-powershell"
        executable.write_text("#!/usr/bin/env python3\nimport os,sys\nfrom pathlib import Path\n"
                              "args=sys.argv[1:]\n"
                              "path=Path(args[args.index('-File')+1])\n"
                              "raw=path.read_bytes()\n"
                              "assert raw.startswith(b'\\xef\\xbb\\xbf')\n"
                              "os.write(1,raw[3:])\n", encoding="utf-8")
        executable.chmod(0o755)
        self.write_config(powershell_executable=str(executable))
        script = "# 中文 long script\n" + ("# padding\n" * 1800) + "Write-Output 'done'\n"
        command = {"mode": "powershell", "script": script, "cwd": str(self.root)}
        self.submit("direct-long-script", "command.execute", command=command)
        self.call("serve", "--once")
        self.assertEqual(self.result("direct-long-script")["state"], "completed")
        output = self.root / "long-powershell.bin"
        self.call("output", "direct-long-script", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), script.encode("utf-8"))
        self.assertEqual(self.calls_list(), [])

    @unittest.skipIf(os.name == "nt", "test hook uses a POSIX shell")
    def test_failed_command_output_push_retries_delivery_only(self) -> None:
        marker = self.root / "push-calls"
        hook = self.remote / "hooks" / "pre-receive"
        source = ("import os,sys; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "h=Path(sys.argv[2]); h.write_text('#!/bin/sh\\nexit 1\\n'); h.chmod(0o755); "
                  "os.write(1,b'unchanged')")
        command = {"mode": "argv", "argv": [sys.executable, "-c", source, str(marker), str(hook)],
                   "cwd": str(self.root)}
        self.submit("direct-push", "command.execute", command=command)
        self.assertNotEqual(self.call("serve", "--once", check=False).returncode, 0)
        self.assertEqual(marker.read_bytes(), b"X")
        hook.unlink()
        self.call("serve", "--once")
        self.assertEqual(self.result("direct-push")["state"], "completed")
        output = self.root / "retry.bin"
        self.call("output", "direct-push", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), b"unchanged")
        self.assertEqual(marker.read_bytes(), b"X")
        self.assertEqual(self.calls_list(), [])

    def test_timeout_is_unknown_and_never_retried(self) -> None:
        self.submit("new", "session.new", "project")
        self.call("serve", "--once")
        self.submit("slow", "task.execute", "project", "sleep", timeout_seconds=1)
        self.call("serve", "--once")
        self.assertEqual(self.result("slow")["state"], "unknown")
        self.call("serve", "--once")
        self.assertEqual(self.calls_list(), ["new", "slow"])

    @unittest.skipIf(os.name == "nt", "test hook uses a POSIX shell")
    def test_failed_result_push_retries_delivery_not_execution(self) -> None:
        self.submit("new", "session.new", "project")
        self.call("serve", "--once")
        self.submit("run", "task.execute", "project", "reject-result")
        self.assertNotEqual(self.call("serve", "--once", check=False).returncode, 0)
        self.assertEqual(self.calls_list(), ["new", "run"])
        (self.remote / "hooks" / "pre-receive").unlink()
        self.call("serve", "--once")
        self.assertEqual(self.result("run")["state"], "completed")
        self.assertEqual(self.calls_list(), ["new", "run"])

    @unittest.skipIf(os.name == "nt", "test process-group shutdown uses POSIX signals")
    def test_service_crash_marks_started_task_unknown(self) -> None:
        self.submit("new", "session.new", "project")
        self.call("serve", "--once")
        self.submit("interrupted", "task.execute", "project", "sleep", timeout_seconds=30)
        process = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(self.config),
                                    "serve", "--once"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 15
        while "interrupted" not in self.calls_list() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("interrupted", self.calls_list())
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        self.call("serve", "--once")
        result = self.result("interrupted")
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(result["native_session_id"])
        self.assertEqual(self.calls_list(), ["new", "interrupted"])

    def test_request_file_modification_stops_processing(self) -> None:
        self.submit("new", "session.new", "project")
        self.call("serve", "--once")
        checkout = self.root / "manual"
        git("clone", "--branch", "pingpong/requests", str(self.remote), str(checkout))
        git("config", "user.name", "Test User", cwd=checkout)
        git("config", "user.email", "test@example.invalid", cwd=checkout)
        request_file = checkout / "pingpong" / "requests" / "new.json"
        request_file.write_text("{}\n", encoding="utf-8")
        git("add", "pingpong/requests/new.json", cwd=checkout)
        git("commit", "-m", "alter request", cwd=checkout)
        git("push", "origin", "pingpong/requests", cwd=checkout)
        result = self.call("serve", "--once", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("append-only", result.stderr)
        self.assertEqual(self.calls_list(), ["new"])

    def test_result_branch_rewrite_stops_processing(self) -> None:
        self.submit("new", "session.new", "project")
        self.call("serve", "--once")
        self.assertEqual(self.result("new")["state"], "completed")
        initial = git("rev-parse", "HEAD", cwd=self.seed)
        git("push", "--force", "origin", initial + ":refs/heads/pingpong/results", cwd=self.seed)
        self.submit("pending", "task.execute", "project", "inspect")
        result = self.call("serve", "--once", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("result branch was rewritten", result.stderr)
        self.assertEqual(self.calls_list(), ["new"])

    def test_state_directory_allows_only_one_service(self) -> None:
        self.submit("lock-probe", "session.new", "project")
        process = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(self.config), "serve"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if "lock-probe" in self.calls_list():
                    break
                self.assertIsNone(process.poll(), "background service exited before handling the task")
                time.sleep(0.05)
            self.assertIn("lock-probe", self.calls_list())
            result = self.call("serve", "--once", check=False)
            self.assertIn("already holds", result.stderr)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_first_start_defaults_to_future_requests_only(self) -> None:
        self.submit("old", "session.new", "old-session")
        self.state = self.root / "new-state"
        self.write_config()
        self.call("serve", "--once")
        self.assertEqual(self.result("old")["state"], "not_found")
        self.assertEqual(self.calls_list(), [])
        self.submit("new", "session.new", "new-session")
        self.call("serve", "--once")
        self.assertEqual(self.calls_list(), ["new"])

    @unittest.skipIf(os.name == "nt", "test wrapper uses a POSIX shebang")
    def test_git_uses_the_configured_local_global_file(self) -> None:
        git_config = self.root / "scoped-gitconfig"
        git_config.write_text("[http]\n\tsslVerify = true\n", encoding="utf-8")
        env_log = self.root / "git-config-paths"
        wrapper = self.root / "git-wrapper"
        wrapper.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                           "with open(" + repr(str(env_log)) + ", 'a') as out:\n"
                           "    out.write(os.environ.get('GIT_CONFIG_GLOBAL', '') + '\\n')\n"
                           "os.execv('/usr/bin/git', ['git'] + sys.argv[1:])\n", encoding="utf-8")
        wrapper.chmod(0o755)
        self.write_config(git_executable=str(wrapper), git_config_global=str(git_config))
        self.submit("scoped", "session.new", "scoped")
        self.call("serve", "--once")
        self.assertEqual(self.result("scoped")["state"], "completed")
        paths = env_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(paths)
        self.assertEqual(set(paths), {str(git_config.resolve())})
        adapter_paths = self.calls.with_suffix(".gitconfig").read_text(encoding="utf-8").splitlines()
        self.assertEqual(adapter_paths, [str(git_config.resolve())])


if __name__ == "__main__":
    unittest.main()
