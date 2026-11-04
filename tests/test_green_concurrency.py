"""Exercise concurrent direct commands and Git result publication locally."""
import base64
import concurrent.futures
import hashlib
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge
from tests.test_green_bridge import FAKE_ADAPTER, git

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "green_bridge.py"


class GreenConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pingpong-concurrency-test-")
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.control = self.root / "control.git"
        self.state = self.root / "state"
        self.calls = self.root / "calls"
        self.fake = self.root / "adapter.py"
        self.config = self.root / "config.json"
        self.process = None
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
        config = {"git_dir": str(self.control), "state_dir": str(self.state), "remote": "origin",
                  "requests_branch": "pingpong/requests", "results_branch": "pingpong/results",
                  "adapter_argv": [sys.executable, str(self.fake), str(self.calls), str(self.remote)],
                  "poll_seconds": 1, "default_timeout_seconds": 25, "max_timeout_seconds": 40,
                  "start_after": "HEAD", "concurrent_tasks": True, "command_workers": 2,
                  "publish_workers": 2, "result_confirmation": "push_ack"}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.call("serve", "--once")

    def tearDown(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.temporary.cleanup()

    def call(self, *arguments: str) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(BRIDGE), "--config", str(self.config), *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if result.returncode:
            self.fail("bridge failed: " + result.stderr)
        return result

    def start_service(self, process_group: bool = False) -> None:
        self.process = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(self.config), "serve"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        start_new_session=process_group)

    def start_service_with_local_atomic_api(self, extra_env=None) -> None:
        environment = os.environ.copy()
        if extra_env:
            environment.update(extra_env)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.support_atomic_server", "--config", str(self.config), "serve"],
            cwd=str(BRIDGE.parents[1]), env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def submit(self, task_id: str, action: str, **details: object) -> None:
        request = {"version": 1, "id": task_id, "action": action, **details}
        path = self.root / (task_id + ".json")
        path.write_text(json.dumps(request), encoding="utf-8")
        self.call("submit", str(path))

    def wait_file(self, path: Path, seconds: int = 15) -> None:
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                self.fail("service exited before the expected file appeared")
            time.sleep(0.05)
        self.assertTrue(path.exists(), "expected file did not appear: " + str(path))

    def wait_result(self, task_id: str, seconds: int = 20) -> dict:
        deadline = time.monotonic() + seconds
        path = "refs/heads/pingpong/results:pingpong/results/" + task_id + ".json"
        while time.monotonic() < deadline:
            result = subprocess.run(["git", "--git-dir", str(self.remote), "show", path],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if result.returncode == 0:
                value = json.loads(result.stdout)
                if value["state"] in bridge.TERMINAL_STATES:
                    return value
            if self.process is not None and self.process.poll() is not None:
                self.fail("service exited before publishing the result")
            time.sleep(0.05)
        self.fail("result did not arrive: " + task_id)

    def wait_group_result(self, group: str, task_id: str, seconds: int = 20) -> dict:
        deadline = time.monotonic() + seconds
        path = "refs/heads/pingpong/results:pingpong/task-results/" + group + ".json"
        while time.monotonic() < deadline:
            result = subprocess.run(["git", "--git-dir", str(self.remote), "show", path],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if result.returncode == 0:
                value = json.loads(result.stdout).get("results", {}).get(task_id)
                if value is not None and value.get("state") in bridge.TERMINAL_STATES:
                    return value
            if self.process is not None and self.process.poll() is not None:
                self.fail("service exited before publishing the grouped result")
            time.sleep(.05)
        self.fail("grouped result did not arrive: " + task_id)

    def command(self, source: str, *arguments: str) -> dict:
        return {"mode": "argv", "argv": [sys.executable, "-c", source, *arguments],
                "cwd": str(self.root), "report_running": False}

    def grouped_batch(self, count: int, task: str) -> dict:
        commands = []
        for number in range(count):
            marker = self.root / (task + str(number) + ".count")
            source = ("import sys,time; from pathlib import Path; p=Path(sys.argv[1]); "
                      "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                      "time.sleep(.4); sys.stdout.write(sys.argv[2])")
            commands.append({"version": 1, "id": "step-" + str(number), "action": "command.execute",
                             "command": self.command(source, str(marker), "result-" + str(number))})
        path = self.root / (task + ".json")
        path.write_text(json.dumps({"commands": commands}), encoding="utf-8")
        self.call("submit-batch", str(path), "--task", task, "--fast")
        deadline = time.monotonic() + 35
        result_path = "refs/heads/pingpong/results:pingpong/task-results/" + task + ".json"
        while time.monotonic() < deadline:
            result = subprocess.run(["git", "--git-dir", str(self.remote), "show", result_path],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if result.returncode == 0:
                values = json.loads(result.stdout)["results"]
                if len(values) == count and all(value["state"] in bridge.TERMINAL_STATES for value in values.values()):
                    for number in range(count):
                        value = values["step-" + str(number)]
                        self.assertEqual(value["state"], "completed")
                        self.assertEqual(base64.b64decode(value["command_result"]["stdout"]["inline_base64"]),
                                         ("result-" + str(number)).encode())
                        self.assertEqual((self.root / (task + str(number) + ".count")).read_bytes(), b"X")
                    return values
            if self.process is not None and self.process.poll() is not None:
                self.fail("service exited before publishing the grouped results")
            time.sleep(.05)
        self.fail("grouped results did not arrive")

    def test_service_logs_task_descriptions_without_sync_chatter(self) -> None:
        task = "log-test"
        requests = [
            {"version": 1, "id": "described", "action": "command.execute",
             "description": "检查运行状态\n读取进程信息", "command": self.command("print('FIRST')")},
            {"version": 1, "id": "legacy", "action": "command.execute",
             "command": self.command("print('SECOND')")},
        ]
        path = self.root / "log-batch.json"
        path.write_text(json.dumps({"commands": requests}, ensure_ascii=False), encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, str(BRIDGE), "--config", str(self.config), "serve"],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.call("submit-batch", str(path), "--task", task, "--fast")
        self.assertEqual(self.wait_group_result(task, "described")["state"], "completed")
        self.assertEqual(self.wait_group_result(task, "legacy")["state"], "completed")
        time.sleep(2.5)  # Allow multiple idle synchronizations after both commands finish.
        log = (self.state / "service.log").read_text(encoding="utf-8")
        self.assertIn("执行命令：检查运行状态 读取进程信息 [任务 log-test / 指令 described]", log)
        self.assertIn("执行命令：未填写中文说明 [任务 log-test / 指令 legacy]", log)
        self.assertNotIn("Git branch synchronization took", log)
        self.assertNotIn("FIRST", log)
        self.assertNotIn("SECOND", log)
        self.process.terminate()
        _, stderr = self.process.communicate(timeout=10)
        console = stderr.decode("utf-8")
        self.assertIn("检查运行状态 读取进程信息", console)
        self.assertIn("未填写中文说明", console)
        self.assertNotIn("Git branch synchronization took", console)

    def test_small_grouped_results_publish_together(self) -> None:
        before = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        self.start_service()
        self.grouped_batch(4, "coalesced")
        after = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        commits = int(git("--git-dir", str(self.remote), "rev-list", "--count", before + ".." + after))
        self.assertLess(commits, 4)

    def test_concurrent_service_without_adapter(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["adapter_argv"] = []
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.start_service()
        self.submit("direct-only", "command.execute", command=self.command("print('PINGPONG_OK')"))
        direct = self.wait_result("direct-only")
        self.assertEqual(direct["state"], "completed")
        self.assertEqual(direct["command_result"]["exit_code"], 0)
        output = self.root / "direct-only.txt"
        self.call("output", "direct-only", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), b"PINGPONG_OK\n")

        self.submit("no-adapter", "session.new", session="project")
        agent = self.wait_result("no-adapter")
        self.assertEqual(agent["state"], "failed")
        self.assertIn("adapter_argv", agent["message"])
        self.assertFalse(self.calls.exists())

    def test_background_service_without_adapter(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["background_sync"] = True
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.test_concurrent_service_without_adapter()

    def test_rejected_group_batch_retries_individually_without_reexecution(self) -> None:
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!" + sys.executable + "\n" + r'''import json,subprocess,sys
for line in sys.stdin:
    old,new,ref=line.split()
    if ref!='refs/heads/pingpong/results': continue
    def count(commit):
        p=subprocess.run(['git','show',commit+':pingpong/task-results/retry-batch.json'],capture_output=True)
        return len(json.loads(p.stdout)['results']) if p.returncode==0 else 0
    if count(new)-count(old)>1: sys.exit(1)
''', encoding="utf-8")
        hook.chmod(0o755)
        before = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        self.start_service()
        self.grouped_batch(2, "retry-batch")
        after = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        self.assertEqual(int(git("--git-dir", str(self.remote), "rev-list", "--count", before + ".." + after)), 2)

    def test_short_command_finishes_while_long_command_is_running(self) -> None:
        started, release, finished = (self.root / name for name in ("started", "release", "finished"))
        long_source = ("import sys,time; from pathlib import Path; "
                       "Path(sys.argv[1]).write_bytes(b'X'); "
                       "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); "
                       "Path(sys.argv[3]).write_bytes(b'X'); sys.stdout.write('LONG')")
        short_source = "import sys; sys.stdout.write('SHORT')"
        self.start_service()
        self.submit("long", "command.execute", command=self.command(
            long_source, str(started), str(release), str(finished)))
        self.wait_file(started)
        self.submit("inspect", "command.execute", command=self.command(short_source))
        try:
            result = self.wait_result("inspect")
            self.assertEqual(result["state"], "completed")
            self.assertFalse(finished.exists(), "the long command finished before the inspection")
        finally:
            release.write_bytes(b"go")
        self.assertEqual(self.wait_result("long")["state"], "completed")
        self.assertEqual(started.read_bytes(), b"X")
        self.assertEqual(finished.read_bytes(), b"X")
        self.assertFalse(self.calls.exists(), "direct commands must not invoke the agent")

    @unittest.skipIf(os.name == "nt", "the blocking hook uses a POSIX shell")
    def test_running_status_upload_does_not_delay_the_command_or_overwrite_its_result(self) -> None:
        started, command_release = self.root / "async-started", self.root / "async-command-release"
        upload_started, upload_release = self.root / "async-upload-started", self.root / "async-upload-release"
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nread old new ref\n"
            "subject=$(git show -s --format=%s \"$new\")\n"
            "if [ \"$subject\" = 'pingpong: update pingpong/results/async-status.json' ]; then\n"
            "  : > " + shlex.quote(str(upload_started)) + "\n"
            "  while [ ! -e " + shlex.quote(str(upload_release)) + " ]; do sleep 0.1; done\n"
            "fi\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        source = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'X'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); "
                  "sys.stdout.write('DONE')")
        command = self.command(source, str(started), str(command_release))
        command["report_running"] = True
        self.submit("async-status", "command.execute", command=command)
        self.start_service()
        try:
            self.wait_file(started)
            self.wait_file(upload_started)
            command_release.write_bytes(b"go")
            deadline = time.monotonic() + 10
            state = ""
            while time.monotonic() < deadline:
                with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                    state = connection.execute("SELECT state FROM jobs WHERE id='async-status'").fetchone()[0]
                if state == "completed":
                    break
                time.sleep(0.05)
            self.assertEqual(state, "completed")
            self.assertFalse(upload_release.exists())
        finally:
            upload_release.write_bytes(b"go")
            command_release.write_bytes(b"go")
        self.assertEqual(self.wait_result("async-status")["state"], "completed")
        deadline = time.monotonic() + 10
        published = ""
        while time.monotonic() < deadline:
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                published = connection.execute("SELECT published_state FROM jobs WHERE id='async-status'").fetchone()[0]
            if published == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(published, "completed")

    def test_three_commands_run_and_publish_independently(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["command_workers"] = 3
        self.config.write_text(json.dumps(config), encoding="utf-8")
        release = self.root / "release-three"
        source = ("import sys,time; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); "
                  "sys.stdout.write(sys.argv[3])")
        markers = [self.root / ("three-" + str(number)) for number in range(3)]
        for number, marker in enumerate(markers):
            self.submit("three-" + str(number), "command.execute", command=self.command(
                source, str(marker), str(release), "RESULT-" + str(number)))
        self.start_service()
        try:
            for marker in markers:
                self.wait_file(marker)
        finally:
            release.write_bytes(b"go")
        for number, marker in enumerate(markers):
            result = self.wait_result("three-" + str(number))
            self.assertEqual(result["state"], "completed")
            self.assertEqual(marker.read_bytes(), b"X")
            output = result["command_result"]["stdout"]
            expected = ("RESULT-" + str(number)).encode()
            self.assertEqual(output["sha256"], hashlib.sha256(expected).hexdigest())
            raw = git("--git-dir", str(self.remote), "show",
                      "refs/heads/pingpong/results:" + output["parts"][0]["path"])
            self.assertEqual(raw, expected.decode())
        self.assertFalse(self.calls.exists(), "direct commands must not invoke the agent")

    def test_grouped_batch_runs_independent_commands_concurrently(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["command_workers"] = 3
        self.config.write_text(json.dumps(config), encoding="utf-8")
        release = self.root / "group-batch-release"
        source = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'X'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); "
                  "sys.stdout.write(sys.argv[3])")
        markers = [self.root / ("group-batch-" + str(i)) for i in range(3)]
        requests = [{"version": 1, "id": "step-" + str(i), "action": "command.execute",
                     "command": self.command(source, str(marker), str(release), "RESULT-" + str(i))}
                    for i, marker in enumerate(markers)]
        batch = self.root / "group-batch.json"
        batch.write_text(json.dumps({"commands": requests}), encoding="utf-8")
        self.call("submit-batch", str(batch), "--task", "concurrency", "--fast")
        self.start_service()
        try:
            for marker in markers:
                self.wait_file(marker)
        finally:
            release.write_bytes(b"go")
        deadline = time.monotonic() + 30
        group = {}
        path = "refs/heads/pingpong/results:pingpong/task-results/concurrency.json"
        while time.monotonic() < deadline:
            output = subprocess.run(["git", "--git-dir", str(self.remote), "show", path],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
            if output.returncode == 0:
                group = json.loads(output.stdout).get("results", {})
                if all(group.get("step-" + str(i), {}).get("state") == "completed" for i in range(3)):
                    break
            time.sleep(0.05)
        self.assertEqual(set(group), {"step-0", "step-1", "step-2"})
        for i, marker in enumerate(markers):
            self.assertEqual(marker.read_bytes(), b"X")
            self.assertEqual(group["step-" + str(i)]["state"], "completed")
            output = self.root / ("batch-result-" + str(i))
            self.call("output", "step-" + str(i), "--task", "concurrency", "--stream", "stdout",
                      "--output", str(output))
            self.assertEqual(output.read_bytes(), ("RESULT-" + str(i)).encode())

    def test_github_watcher_keeps_accepting_commands_during_execution(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"watch_mode": "github_api", "reconcile_seconds": 60})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        settings = bridge.load_settings(self.config)
        started, release = self.root / "watch-started", self.root / "watch-release"
        ready, stop = threading.Event(), threading.Event()
        remote = self.remote

        class LocalWatcher:
            next_check_at = 0.0

            def __init__(self, _transport, _settings) -> None:
                pass

            def check(self) -> str:
                if stop.is_set():
                    raise KeyboardInterrupt()
                ready.set()
                return git("--git-dir", str(remote), "rev-parse", "refs/heads/pingpong/requests")

            def close(self) -> None:
                pass

        def run_service() -> None:
            try:
                bridge.serve(settings, False)
            except KeyboardInterrupt:
                return

        source = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'X'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)')")
        with mock.patch.object(bridge, "GitHubRefWatcher", LocalWatcher):
            worker = threading.Thread(target=run_service, daemon=True)
            worker.start()
            try:
                self.assertTrue(ready.wait(10), "watcher did not start")
                self.submit("watch-long", "command.execute", command=self.command(source, str(started), str(release)))
                self.wait_file(started)
                self.submit("watch-short", "command.execute", command=self.command("import sys; sys.stdout.write('OK')"))
                self.assertEqual(self.wait_result("watch-short")["state"], "completed")
            finally:
                release.write_bytes(b"go")
                stop.set()
                worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "service did not stop")
        self.assertEqual(started.read_bytes(), b"X")

    def test_two_result_pushes_resolve_a_real_branch_race(self) -> None:
        settings = bridge.load_settings(self.config)
        first = bridge.ConcurrentGitTransport(settings)
        second = bridge.ConcurrentGitTransport(settings)
        base = first.branch_tip(settings.results_branch)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        raced = set()
        conflicts = []
        original = bridge.GitTransport.push_commit

        def racing_push(transport, branch, parent, commit):
            name = threading.current_thread().name
            with lock:
                should_wait = parent == base and name not in raced
                if should_wait:
                    raced.add(name)
            if should_wait:
                barrier.wait(timeout=10)
            try:
                return original(transport, branch, parent, commit)
            except bridge.PushError:
                with lock:
                    conflicts.append(name)
                raise

        with mock.patch.object(bridge.GitTransport, "push_commit", racing_push):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                jobs = [pool.submit(transport.publish, settings.results_branch, "outputs/" + name,
                                    name.encode(), True, None, base, False)
                        for transport, name in ((first, "one"), (second, "two"))]
                for job in jobs:
                    job.result(timeout=25)
        self.assertEqual(len(raced), 2)
        self.assertTrue(conflicts, "the test did not create a push conflict")
        for name in ("one", "two"):
            self.assertEqual(git("--git-dir", str(self.remote), "show",
                                 "refs/heads/pingpong/results:outputs/" + name), name)

    @unittest.skipIf(os.name == "nt", "the blocking hook uses a POSIX shell")
    def test_slow_publication_does_not_block_another_result(self) -> None:
        started, release = self.root / "publish-started", self.root / "publish-release"
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nread old new ref\n"
            "subject=$(git show -s --format=%s \"$new\")\n"
            "if [ \"$subject\" = 'pingpong: command slow-pub' ]; then\n"
            "  : > " + shlex.quote(str(started)) + "\n"
            "  while [ ! -e " + shlex.quote(str(release)) + " ]; do sleep 0.1; done\n"
            "fi\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        self.start_service()
        self.submit("slow-pub", "command.execute", command=self.command("import sys; sys.stdout.write('SLOW')"))
        self.wait_file(started)
        self.submit("fast-pub", "command.execute", command=self.command("import sys; sys.stdout.write('FAST')"))
        try:
            self.assertEqual(self.wait_result("fast-pub")["state"], "completed")
            self.assertFalse(release.exists(), "the first publication was released too early")
        finally:
            release.write_bytes(b"go")
        self.assertEqual(self.wait_result("slow-pub")["state"], "completed")

    @unittest.skipIf(os.name == "nt", "the rejection hook uses a POSIX shell")
    def test_failed_result_push_retries_without_reexecuting_command(self) -> None:
        marker = self.root / "executed"
        hook = self.remote / "hooks" / "pre-receive"
        source = ("import sys; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "h=Path(sys.argv[2]); h.write_text('#!/bin/sh\\necho synthetic-secret >&2\\nexit 1\\n'); h.chmod(0o755); "
                  "sys.stdout.write('saved')")
        self.submit("retry", "command.execute", command=self.command(source, str(marker), str(hook)))
        self.start_service()
        self.wait_file(marker)
        deadline = time.monotonic() + 15
        log = self.state / "service.log"
        while time.monotonic() < deadline:
            if log.exists() and "Result publication retry failed" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        self.assertIn("Result publication retry failed", log.read_text(encoding="utf-8"))
        self.assertNotIn("synthetic-secret", log.read_text(encoding="utf-8"))
        with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
            delivery = connection.execute("SELECT last_error, failures, next_retry_at FROM delivery_attempts "
                                          "WHERE job_id='retry'").fetchone()
        self.assertEqual(delivery[0], "remote_rejected")
        self.assertGreaterEqual(delivery[1], 1)
        self.assertGreater(delivery[2], time.time())
        deadline = time.monotonic() + 15
        while delivery[1] < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                delivery = connection.execute("SELECT last_error, failures, next_retry_at FROM delivery_attempts "
                                              "WHERE job_id='retry'").fetchone()
        self.assertGreaterEqual(delivery[1], 2)
        self.assertEqual(log.read_text(encoding="utf-8").count("Result publication retry failed"), 1)
        hook.unlink()
        self.assertEqual(self.wait_result("retry")["state"], "completed")
        self.assertEqual(marker.read_bytes(), b"X")

    @unittest.skipIf(os.name == "nt", "the size-rejecting hook uses a POSIX shell")
    def test_large_result_recovers_in_small_commits_without_reexecution(self) -> None:
        marker = self.root / "large-output-calls"
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nread old new ref\n"
            "for path in $(git diff-tree -r --no-commit-id --name-only \"$old\" \"$new\" -- pingpong/outputs/); do\n"
            "  size=$(git cat-file -s \"$new:$path\") || exit 1\n"
            "  if [ \"$size\" -gt 70000 ]; then exit 1; fi\n"
            "done\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        source = ("import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1,bytes(range(256))*800)")
        self.submit("large-recover", "command.execute", command=self.command(source, str(marker)))
        self.start_service()
        result = self.wait_result("large-recover", seconds=40)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(marker.read_bytes(), b"X")
        parts = result["command_result"]["stdout"]["parts"]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part["size"] <= bridge.RECOVERY_CHUNK_BYTES for part in parts))
        output = self.root / "recovered.bin"
        self.call("output", "large-recover", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), bytes(range(256)) * 800)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), result["command_result"]["stdout"]["sha256"])

    def test_rewritten_result_branch_pauses_new_commands(self) -> None:
        original = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        self.start_service()
        self.submit("before-rewrite", "command.execute", command=self.command("import sys; sys.stdout.write('OK')"))
        self.assertEqual(self.wait_result("before-rewrite")["state"], "completed")
        deadline = time.monotonic() + 10
        published = ""
        while time.monotonic() < deadline:
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                published = connection.execute(
                    "SELECT published_state FROM jobs WHERE id = ?", ("before-rewrite",)).fetchone()[0]
            if published == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(published, "completed")
        self.process.terminate()
        self.process.wait(timeout=10)
        self.process = None
        git("--git-dir", str(self.remote), "update-ref", "refs/heads/pingpong/results", original)
        marker = self.root / "must-not-run"
        source = "import sys; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'X')"
        self.submit("after-rewrite", "command.execute", command=self.command(source, str(marker)))
        self.start_service()
        log = self.state / "service.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "result branch was rewritten" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        self.assertIn("result branch was rewritten", log.read_text(encoding="utf-8"))
        self.assertFalse(marker.exists(), "a command started after the branch rewrite")

    @unittest.skipIf(os.name == "nt", "the synthetic Git wrapper uses POSIX process replacement")
    def test_background_fetch_does_not_block_finished_result(self) -> None:
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        block, stalled, release = (self.root / name for name in ("block-fetch", "fetch-stalled", "fetch-release"))
        started, finish = (self.root / name for name in ("command-started", "command-finish"))
        wrapper = self.root / "git-wrapper"
        wrapper.write_text(
            "#!" + sys.executable + "\n" +
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            "real=" + repr(real_git) + "\n"
            "block=Path(" + repr(str(block)) + ")\nstalled=Path(" + repr(str(stalled)) +
            ")\nrelease=Path(" + repr(str(release)) + ")\n"
            "if 'fetch' in sys.argv[1:] and block.exists() and not release.exists():\n"
            " p=subprocess.run([real]+sys.argv[1:]); stalled.write_bytes(b'1')\n"
            " while not release.exists(): time.sleep(.05)\n"
            " sys.exit(p.returncode)\n"
            "os.execv(real,[real]+sys.argv[1:])\n", encoding="utf-8")
        wrapper.chmod(0o755)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True, "git_executable": str(wrapper)})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        source = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'1'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); sys.stdout.write('OK')")
        request = {"version": 1, "id": "during-fetch", "action": "command.execute",
                   "command": self.command(source, str(started), str(finish))}
        path = self.root / "during-fetch.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.start_service_with_local_atomic_api()
        self.call("submit", str(path), "--task", "during-fetch-group", "--fast")
        self.wait_file(started)
        block.write_bytes(b"1")
        try:
            self.wait_file(stalled)
            finish.write_bytes(b"1")
            result = self.wait_group_result("during-fetch-group", "during-fetch", seconds=10)
            self.assertEqual(result["state"], "completed")
            self.assertFalse(release.exists(), "the fetch was released before the result was published")
        finally:
            release.write_bytes(b"1")
        self.submit("after-fetch", "command.execute", command=self.command("print('AFTER')"))
        self.assertEqual(self.wait_result("after-fetch")["state"], "completed")
        self.assertNotIn("result branch was rewritten", (self.state / "service.log").read_text(encoding="utf-8"))

    def test_background_sync_rejects_a_rewritten_result_branch(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["background_sync"] = True
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.test_rewritten_result_branch_pauses_new_commands()

    def test_background_sync_keeps_github_watch_working(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["background_sync"] = True
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.test_github_watcher_keeps_accepting_commands_during_execution()

    def test_atomic_ack_loss_confirms_result_without_reexecution(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        marker, calls = self.root / "lost-ack", self.root / "execution-count"
        marker.write_bytes(b"1")
        source = ("import sys; from pathlib import Path; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); print('OK')")
        request = {"version": 1, "id": "lost-ack-job", "action": "command.execute",
                   "command": self.command(source, str(calls))}
        path = self.root / "lost-ack-job.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.start_service_with_local_atomic_api({"PINGPONG_TEST_LOST_ACK": str(marker)})
        self.call("submit", str(path), "--task", "lost-ack-group", "--fast")
        result = self.wait_group_result("lost-ack-group", "lost-ack-job")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(base64.b64decode(result["command_result"]["stdout"]["inline_base64"]), b"OK\n")
        task_id = bridge.group_job_id("lost-ack-group", "lost-ack-job")
        deadline = time.monotonic() + 20
        published = None
        while time.monotonic() < deadline:
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                row = connection.execute("SELECT published_state FROM jobs WHERE id = ?", (task_id,)).fetchone()
            published = row[0] if row else None
            if published == "completed":
                break
            time.sleep(.05)
        self.assertEqual(published, "completed", "the result was not confirmed after the simulated loss")
        self.assertFalse(marker.exists(), "the test did not simulate an acknowledgement loss")
        self.assertEqual(calls.read_bytes(), b"X", "the command was executed more than once")
        history = git("--git-dir", str(self.remote), "rev-list", "refs/heads/pingpong/results", "--",
                      "pingpong/task-results/lost-ack-group.json").splitlines()
        self.assertEqual(len(history), 1, "the same result was committed more than once")

    def test_parallel_atomic_groups_resolve_conflicts_without_losing_results(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        barrier = self.root / "atomic-barrier"
        barrier.mkdir()
        for number in (1, 2):
            calls = self.root / ("parallel-calls-" + str(number))
            source = ("import sys; from pathlib import Path; p=Path(sys.argv[1]); "
                      "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); print(sys.argv[2])")
            request = {"version": 1, "id": "parallel-" + str(number), "action": "command.execute",
                       "command": self.command(source, str(calls), str(number))}
            path = self.root / ("parallel-" + str(number) + ".json")
            path.write_text(json.dumps(request), encoding="utf-8")
            self.call("submit", str(path), "--task", "parallel-group-" + str(number), "--fast")
        self.start_service_with_local_atomic_api({"PINGPONG_TEST_CAS_BARRIER": str(barrier)})
        for number in (1, 2):
            result = self.wait_group_result("parallel-group-" + str(number), "parallel-" + str(number), seconds=30)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(base64.b64decode(result["command_result"]["stdout"]["inline_base64"]),
                             (str(number) + "\n").encode())
            self.assertEqual((self.root / ("parallel-calls-" + str(number))).read_bytes(), b"X")
        self.assertEqual(len(list(barrier.iterdir())), 2, "both atomic publishers did not reach the barrier")
        deadline = time.monotonic() + 20
        states = []
        while time.monotonic() < deadline:
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                states = [row[0] for row in connection.execute("SELECT published_state FROM jobs WHERE id IN (?, ?)",
                    (bridge.group_job_id("parallel-group-1", "parallel-1"),
                     bridge.group_job_id("parallel-group-2", "parallel-2")))]
            if states == ["completed", "completed"]:
                break
            time.sleep(.05)
        self.assertEqual(states, ["completed", "completed"])

    def test_atomic_size_rejection_falls_back_without_reexecution(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        calls = self.root / "fallback-calls"
        source = ("import sys; from pathlib import Path; p=Path(sys.argv[1]); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); print('OK')")
        request = {"version": 1, "id": "fallback-job", "action": "command.execute",
                   "command": self.command(source, str(calls))}
        path = self.root / "fallback-job.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.start_service_with_local_atomic_api({"PINGPONG_TEST_CONTENT_LIMIT": "1"})
        self.call("submit", str(path), "--task", "fallback-group", "--fast")
        result = self.wait_group_result("fallback-group", "fallback-job", seconds=30)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(base64.b64decode(result["command_result"]["stdout"]["inline_base64"]), b"OK\n")
        self.assertEqual(calls.read_bytes(), b"X")
        task_id = bridge.group_job_id("fallback-group", "fallback-job")
        deadline = time.monotonic() + 15
        row = None
        while time.monotonic() < deadline:
            with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                row = connection.execute("SELECT jobs.published_state, delivery_attempts.attempts "
                                         "FROM jobs JOIN delivery_attempts ON delivery_attempts.job_id = jobs.id "
                                         "WHERE jobs.id = ?", (task_id,)).fetchone()
            if row and row[0] == "completed":
                break
            time.sleep(.05)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "completed")
        self.assertGreaterEqual(row[1], 2, "the test did not exercise API failure and fallback")

    def test_large_existing_group_skips_api_without_waiting_for_a_retry(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        transport = bridge.GitTransport(bridge.load_settings(self.config))
        group = "already-large"
        content = bridge.json_bytes({"version": 2, "task_id": group,
                                     "results": {"prior": {"padding": "x" * 80000}}})
        base = transport.branch_tip("pingpong/results")
        transport.publish("pingpong/results", "pingpong/task-results/" + group + ".json", content,
                          base_hint=base, confirm_remote=False)
        api_calls = self.root / "unexpected-api-calls"
        self.start_service_with_local_atomic_api({"PINGPONG_TEST_API_CALLS": str(api_calls)})
        request = {"version": 1, "id": "large-group-job", "action": "command.execute",
                   "command": self.command("print('OK')")}
        path = self.root / "large-group-job.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.call("submit", str(path), "--task", group, "--fast")
        result = self.wait_group_result(group, "large-group-job", seconds=20)
        self.assertEqual(result["state"], "completed")
        self.assertFalse(api_calls.exists(), "an oversized group was sent to the API")
        with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
            generation = connection.execute("SELECT value FROM metadata WHERE key = 'atomic_attempt_generation'")
            self.assertIsNone(generation.fetchone(), "a known oversized group started an API attempt")

    def test_atomic_rate_limit_pauses_api_and_falls_back_for_later_jobs(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        marker, calls = self.root / "rate-limit", self.root / "api-calls"
        marker.write_bytes(b"1")
        self.start_service_with_local_atomic_api({"PINGPONG_TEST_RATE_LIMIT": str(marker),
                                                  "PINGPONG_TEST_API_CALLS": str(calls)})
        for number in (1, 2):
            request = {"version": 1, "id": "rate-" + str(number), "action": "command.execute",
                       "command": self.command("print('OK')")}
            path = self.root / ("rate-" + str(number) + ".json")
            path.write_text(json.dumps(request), encoding="utf-8")
            self.call("submit", str(path), "--task", "rate-group-" + str(number), "--fast")
            result = self.wait_group_result("rate-group-" + str(number), "rate-" + str(number), seconds=30)
            self.assertEqual(result["state"], "completed")
        self.assertFalse(marker.exists())
        self.assertEqual(calls.read_bytes(), b"X", "a later job retried the rate-limited API")
        with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
            retry_at = float(connection.execute("SELECT value FROM metadata WHERE key = 'atomic_api_retry_at'")
                             .fetchone()[0])
        self.assertGreater(retry_at, time.time())

    @unittest.skipIf(os.name == "nt", "the synthetic Git wrapper uses POSIX process replacement")
    def test_background_publication_does_not_restore_a_rewritten_branch(self) -> None:
        original = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)
        block, stalled, release = (self.root / name for name in ("race-block", "race-stalled", "race-release"))
        started, finish = (self.root / name for name in ("race-started", "race-finish"))
        wrapper = self.root / "race-git-wrapper"
        wrapper.write_text(
            "#!" + sys.executable + "\n" +
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            "real=" + repr(real_git) + "\n"
            "block=Path(" + repr(str(block)) + ")\nstalled=Path(" + repr(str(stalled)) +
            ")\nrelease=Path(" + repr(str(release)) + ")\n"
            "if 'fetch' in sys.argv[1:] and block.exists() and not release.exists():\n"
            " p=subprocess.run([real]+sys.argv[1:]); stalled.write_bytes(b'1')\n"
            " while not release.exists(): time.sleep(.05)\n"
            " sys.exit(p.returncode)\n"
            "os.execv(real,[real]+sys.argv[1:])\n", encoding="utf-8")
        wrapper.chmod(0o755)
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config.update({"background_sync": True, "api_results": True, "git_executable": str(wrapper)})
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.start_service_with_local_atomic_api()
        self.submit("prime-rewrite-race", "command.execute", command=self.command("print('PRIME')"))
        self.assertEqual(self.wait_result("prime-rewrite-race")["state"], "completed")
        source = ("import sys,time; from pathlib import Path; Path(sys.argv[1]).write_bytes(b'1'); "
                  "exec('while not Path(sys.argv[2]).exists():\\n time.sleep(.05)'); print('OK')")
        request = {"version": 1, "id": "rewrite-race", "action": "command.execute",
                   "command": self.command(source, str(started), str(finish))}
        path = self.root / "rewrite-race.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        self.call("submit", str(path), "--task", "rewrite-group", "--fast")
        self.wait_file(started)
        block.write_bytes(b"1")
        try:
            self.wait_file(stalled)
            git("--git-dir", str(self.remote), "update-ref", "refs/heads/pingpong/results", original)
            finish.write_bytes(b"1")
            task_id = bridge.group_job_id("rewrite-group", "rewrite-race")
            deadline = time.monotonic() + 10
            failures = 0
            while time.monotonic() < deadline:
                with sqlite3.connect(str(self.state / "state.sqlite3")) as connection:
                    row = connection.execute("SELECT failures FROM delivery_attempts WHERE job_id = ?",
                                             (task_id,)).fetchone()
                if row and row[0] > 0:
                    failures = row[0]
                    break
                time.sleep(.05)
            self.assertGreater(failures, 0, "the atomic compare-and-swap was not attempted")
            current = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results")
            self.assertEqual(current, original, "publishing restored a result branch that had been rewritten")
        finally:
            release.write_bytes(b"1")
        log = self.state / "service.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "result branch was rewritten" in log.read_text(encoding="utf-8"):
                break
            time.sleep(.05)
        self.assertIn("result branch was rewritten", log.read_text(encoding="utf-8"))
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/results"), original)

    def test_direct_command_runs_while_agent_task_waits(self) -> None:
        self.submit("new-session", "session.new", session="project")
        self.call("serve", "--once")
        started, release = self.root / "agent-started", self.root / "agent-release"
        self.fake.write_text(
            "import json,sys,time\nfrom pathlib import Path\n"
            "req=json.load(sys.stdin)\n"
            "Path(" + repr(str(started)) + ").write_bytes(b'X')\n"
            "while not Path(" + repr(str(release)) + ").exists(): time.sleep(.05)\n"
            "json.dump({'version':1,'status':'completed','native_session_id':req['native_session_id']},sys.stdout)\n",
            encoding="utf-8")
        self.start_service()
        self.submit("agent-long", "task.execute", session="project", message="wait")
        self.wait_file(started)
        self.submit("direct", "command.execute", command=self.command("import sys; sys.stdout.write('OK')"))
        try:
            self.assertEqual(self.wait_result("direct")["state"], "completed")
        finally:
            release.write_bytes(b"go")
        self.assertEqual(self.wait_result("agent-long")["state"], "completed")

    @unittest.skipIf(os.name == "nt", "test process-group shutdown uses POSIX signals")
    def test_crash_marks_concurrent_started_commands_unknown(self) -> None:
        source = ("import sys,time; from pathlib import Path; "
                  "p=Path(sys.argv[1]); p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "time.sleep(20)")
        markers = [self.root / "crash-one", self.root / "crash-two"]
        for number, marker in enumerate(markers, 1):
            self.submit("crash-" + str(number), "command.execute", command=self.command(source, str(marker)))
        self.start_service(process_group=True)
        for marker in markers:
            self.wait_file(marker)
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)
        self.call("serve", "--once")
        for number, marker in enumerate(markers, 1):
            self.assertEqual(self.wait_result("crash-" + str(number))["state"], "unknown")
            self.assertEqual(marker.read_bytes(), b"X")


if __name__ == "__main__":
    unittest.main()
