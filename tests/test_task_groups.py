"""Exercise grouped tasks without weakening command identity or Git concurrency."""
import concurrent.futures
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from scripts import green_bridge as bridge
from tests.test_green_bridge import FAKE_ADAPTER, git

BRIDGE = Path(__file__).resolve().parents[1] / "scripts" / "green_bridge.py"


class TaskGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pingpong-task-test-")
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.control = self.root / "control.git"
        self.state = self.root / "state"
        self.calls = self.root / "calls"
        self.fake = self.root / "adapter.py"
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
        config = {"git_dir": str(self.control), "state_dir": str(self.state), "remote": "origin",
                  "requests_branch": "pingpong/requests", "results_branch": "pingpong/results",
                  "adapter_argv": [sys.executable, str(self.fake), str(self.calls), str(self.remote)],
                  "poll_seconds": 1, "default_timeout_seconds": 20, "max_timeout_seconds": 30,
                  "start_after": "HEAD", "result_confirmation": "push_ack"}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        self.call("serve", "--once")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(BRIDGE), "--config", str(self.config), *arguments],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
        if check and result.returncode:
            self.fail("bridge failed: " + result.stderr)
        return result

    def request(self, command_id: str, source: str) -> dict:
        return {"version": 1, "id": command_id, "action": "command.execute",
                "command": {"mode": "argv", "argv": [sys.executable, "-c", source],
                            "cwd": str(self.root), "report_running": False}}

    def submit(self, request: dict, task: str = "project") -> subprocess.CompletedProcess:
        path = self.root / (request["id"] + ".json")
        path.write_text(json.dumps(request), encoding="utf-8")
        return self.call("submit", str(path), "--task", task, "--fast")

    def test_commands_share_files_and_keep_exact_output_and_deduplication(self) -> None:
        marker = self.root / "calls.txt"
        source = ("import sys; from pathlib import Path; p=Path(" + repr(str(marker)) + "); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "sys.stdout.buffer.write(b'raw\\x00bytes')")
        first = self.request("group-one", source)
        second = self.request("group-two", "print('second')")
        self.submit(first)
        self.submit(first)
        self.submit(second)
        self.call("serve", "--once")
        self.call("serve", "--once")
        self.assertEqual(marker.read_bytes(), b"X")
        for ident in ("group-one", "group-two"):
            result = json.loads(self.call("status", ident, "--task", "project").stdout)
            self.assertEqual(result["state"], "completed")
            timing = result["timing"]
            queued = datetime.datetime.fromisoformat(timing["queued_at"])
            running = datetime.datetime.fromisoformat(timing["running_at"])
            finished = datetime.datetime.fromisoformat(timing["finished_at"])
            self.assertLessEqual(queued, running)
            self.assertLessEqual(running, finished)
        output = self.root / "output.bin"
        self.call("output", "group-one", "--task", "project", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), b"raw\x00bytes")
        requests = json.loads(git("--git-dir", str(self.remote), "show",
                                  "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        results = json.loads(git("--git-dir", str(self.remote), "show",
                                 "refs/heads/pingpong/results:pingpong/task-results/project.json"))
        self.assertEqual([r["id"] for r in requests["commands"]], ["group-one", "group-two"])
        self.assertEqual(set(results["results"]), {"group-one", "group-two"})
        self.assertIn("inline_base64", results["results"]["group-one"]["command_result"]["stdout"])
        self.assertEqual(results["results"]["group-one"]["command_result"]["stdout"]["parts"], [])
        self.assertEqual(results["results"]["group-one"]["command_result"]["stderr"]["parts"], [])
        empty = self.root / "empty.bin"
        self.call("output", "group-one", "--task", "project", "--stream", "stderr", "--output", str(empty))
        self.assertEqual(empty.read_bytes(), b"")
        self.assertFalse(self.calls.exists())
        changed = self.request("group-one", "print('different')")
        path = self.root / "changed.json"
        path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertNotEqual(self.call("submit", str(path), "--task", "project", "--fast", check=False).returncode, 0)

    def test_altering_a_previous_command_stops_new_execution(self) -> None:
        self.submit(self.request("first", "print('first')"))
        self.call("serve", "--once")
        checkout = self.root / "checkout"
        git("clone", "--branch", "pingpong/requests", str(self.remote), str(checkout))
        git("config", "user.name", "Test User", cwd=checkout)
        git("config", "user.email", "test@example.invalid", cwd=checkout)
        path = checkout / "pingpong" / "task-requests" / "project.json"
        group = json.loads(path.read_text(encoding="utf-8"))
        group["commands"][0]["command"]["argv"][-1] = "print('tampered')"
        group["commands"].append(self.request("new", "print('new')"))
        path.write_text(json.dumps(group), encoding="utf-8")
        git("add", "pingpong/task-requests/project.json", cwd=checkout)
        git("commit", "-m", "invalid edit", cwd=checkout)
        git("push", "origin", "pingpong/requests", cwd=checkout)
        result = self.call("serve", "--once", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existing task commands cannot be changed", result.stderr)
        self.assertEqual(json.loads(self.call("status", "new", "--task", "project").stdout)["state"], "not_found")

    def test_same_command_label_in_two_tasks_has_independent_identity(self) -> None:
        self.submit(self.request("step-1", "print('one')"), task="project-one")
        self.submit(self.request("step-1", "print('two')"), task="project-two")
        self.call("serve", "--once")
        for task, expected in (("project-one", b"one\n"), ("project-two", b"two\n")):
            result = json.loads(self.call("status", "step-1", "--task", task).stdout)
            self.assertEqual(result["state"], "completed")
            output = self.root / (task + ".bin")
            self.call("output", "step-1", "--task", task, "--stream", "stdout", "--output", str(output))
            self.assertEqual(output.read_bytes(), expected)

    def test_batch_adds_independent_commands_in_one_commit_and_is_idempotent(self) -> None:
        base = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests")
        batch = {"commands": [self.request("batch-%d" % i, "print(%d)" % i) for i in range(3)]}
        path = self.root / "batch.json"
        path.write_text(json.dumps(batch), encoding="utf-8")
        first = json.loads(self.call("submit-batch", str(path), "--task", "project", "--fast").stdout)
        repeated = json.loads(self.call("submit-batch", str(path), "--task", "project", "--fast").stdout)
        self.assertEqual(first, repeated)
        head = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests")
        self.assertEqual(git("--git-dir", str(self.remote), "rev-list", "--count", base + ".." + head), "1")
        self.call("serve", "--once")
        for request in batch["commands"]:
            result = json.loads(self.call("status", request["id"], "--task", "project").stdout)
            self.assertEqual(result["state"], "completed")

    def test_tampered_inline_output_is_rejected(self) -> None:
        self.submit(self.request("inline", "print('original')"))
        self.call("serve", "--once")
        checkout = self.root / "result-checkout"
        git("clone", "--branch", "pingpong/results", str(self.remote), str(checkout))
        git("config", "user.name", "Test User", cwd=checkout)
        git("config", "user.email", "test@example.invalid", cwd=checkout)
        path = checkout / "pingpong" / "task-results" / "project.json"
        group = json.loads(path.read_text(encoding="utf-8"))
        group["results"]["inline"]["command_result"]["stdout"]["inline_base64"] = "Y2hhbmdlZA=="
        path.write_text(json.dumps(group), encoding="utf-8")
        git("add", "pingpong/task-results/project.json", cwd=checkout)
        git("commit", "-m", "tamper inline output", cwd=checkout)
        git("push", "origin", "pingpong/results", cwd=checkout)
        output = self.root / "tampered.bin"
        result = self.call("output", "inline", "--task", "project", "--stream", "stdout",
                           "--output", str(output), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_optional_agent_actions_keep_their_named_session_in_a_task(self) -> None:
        for request in ({"version": 1, "id": "new", "action": "session.new", "session": "project"},
                        {"version": 1, "id": "run", "action": "task.execute", "session": "project",
                         "message": "continue"}):
            path = self.root / (request["id"] + ".json")
            path.write_text(json.dumps(request), encoding="utf-8")
            self.call("submit", str(path), "--task", "project")
        self.call("serve", "--once")
        result = json.loads(self.call("status", "run", "--task", "project").stdout)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["session"], "project")
        self.assertEqual(self.calls.read_text(encoding="utf-8").splitlines(),
                         [bridge.group_job_id("project", "new"), bridge.group_job_id("project", "run")])
        inputs = [json.loads(line) for line in self.calls.with_suffix(".inputs").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(inputs[-1]["context"]["pingpong_relay"], {"task_id": "project", "command_id": "run"})

    def test_two_concurrent_appends_preserve_both_commands(self) -> None:
        settings = bridge.load_settings(self.config)
        first, second = bridge.ConcurrentGitTransport(settings), bridge.ConcurrentGitTransport(settings)
        base = first.branch_tip(settings.requests_branch)
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
                futures = [pool.submit(transport.append_group_request, "project", self.request(name, "pass"), base, False)
                           for transport, name in ((first, "one"), (second, "two"))]
                for future in futures:
                    future.result(timeout=25)
        self.assertTrue(conflicts)
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        self.assertEqual({r["id"] for r in group["commands"]}, {"one", "two"})

    def test_fast_submit_reuses_an_api_confirmed_local_commit(self) -> None:
        self.submit(self.request("first", "pass"))
        settings = bridge.load_settings(self.config)
        request_path = self.root / "second.json"
        request_path.write_text(json.dumps(self.request("second", "pass")), encoding="utf-8")
        remote_head = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests").strip()
        with mock.patch.object(bridge, "GitHubRefWatcher") as watcher:
            watcher.return_value.check.return_value = remote_head
            with mock.patch.object(bridge.GitTransport, "branch_tip", side_effect=AssertionError("unexpected fetch")):
                bridge.submit(settings, request_path, fast=True, group_id="project")
        watcher.return_value.close.assert_called_once()
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        self.assertEqual([item["id"] for item in group["commands"]], ["first", "second"])

    def test_fast_submit_fetches_an_unseen_head_without_losing_other_changes(self) -> None:
        self.submit(self.request("first", "pass"))
        checkout = self.root / "other-client"
        git("clone", "--branch", "pingpong/requests", str(self.remote), str(checkout))
        git("config", "user.name", "Other User", cwd=checkout)
        git("config", "user.email", "other@example.invalid", cwd=checkout)
        (checkout / "other.txt").write_text("preserve\n", encoding="utf-8")
        git("add", "other.txt", cwd=checkout)
        git("commit", "-m", "other client", cwd=checkout)
        git("push", "origin", "pingpong/requests", cwd=checkout)
        remote_head = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests").strip()
        request_path = self.root / "second.json"
        request_path.write_text(json.dumps(self.request("second", "pass")), encoding="utf-8")
        with mock.patch.object(bridge, "GitHubRefWatcher") as watcher:
            watcher.return_value.check.return_value = remote_head
            bridge.submit(bridge.load_settings(self.config), request_path, fast=True, group_id="project")
        self.assertEqual(git("--git-dir", str(self.remote), "show",
                             "refs/heads/pingpong/requests:other.txt"), "preserve")
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        self.assertEqual([item["id"] for item in group["commands"]], ["first", "second"])

    def test_fast_submit_rejects_an_observed_branch_rewind(self) -> None:
        initial = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests").strip()
        self.submit(self.request("first", "pass"))
        git("--git-dir", str(self.remote), "update-ref", "refs/heads/pingpong/requests", initial)
        request_path = self.root / "second.json"
        request_path.write_text(json.dumps(self.request("second", "pass")), encoding="utf-8")
        with mock.patch.object(bridge, "GitHubRefWatcher") as watcher:
            watcher.return_value.check.return_value = initial
            with self.assertRaisesRegex(bridge.BridgeError, "rewritten"):
                bridge.submit(bridge.load_settings(self.config), request_path, fast=True, group_id="project")
        self.assertEqual(git("--git-dir", str(self.remote), "rev-parse",
                             "refs/heads/pingpong/requests").strip(), initial)

    def test_stale_api_response_is_checked_against_git_before_rejecting(self) -> None:
        initial = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests").strip()
        self.submit(self.request("first", "pass"))
        request_path = self.root / "second.json"
        request_path.write_text(json.dumps(self.request("second", "pass")), encoding="utf-8")
        with mock.patch.object(bridge, "GitHubRefWatcher") as watcher:
            watcher.return_value.check.return_value = initial
            bridge.submit(bridge.load_settings(self.config), request_path, fast=True, group_id="project")
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        self.assertEqual([item["id"] for item in group["commands"]], ["first", "second"])

    def test_fast_submit_falls_back_to_git_when_api_is_unavailable(self) -> None:
        self.submit(self.request("first", "pass"))
        request_path = self.root / "second.json"
        request_path.write_text(json.dumps(self.request("second", "pass")), encoding="utf-8")
        with mock.patch.object(bridge, "GitHubRefWatcher", side_effect=bridge.BridgeError("unavailable")):
            bridge.submit(bridge.load_settings(self.config), request_path, fast=True, group_id="project")
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/requests:pingpong/task-requests/project.json"))
        self.assertEqual([item["id"] for item in group["commands"]], ["first", "second"])

    def test_independent_group_watermark_recovers_requests_skipped_by_older_service(self) -> None:
        self.submit(self.request("after-rollback", "print('ok')"))
        head = git("--git-dir", str(self.remote), "rev-parse", "refs/heads/pingpong/requests")
        connection = bridge.connect_database(self.state / "state.sqlite3")
        try:
            with connection:
                bridge.set_metadata(connection, "request_head", head)
        finally:
            connection.close()
        self.call("serve", "--once")
        result = json.loads(self.call("status", "after-rollback", "--task", "project").stdout)
        self.assertEqual(result["state"], "completed")

    def test_merge_commit_is_rejected_even_when_it_changes_no_task_file(self) -> None:
        checkout = self.root / "merge-checkout"
        git("clone", "--branch", "pingpong/requests", str(self.remote), str(checkout))
        git("config", "user.name", "Test User", cwd=checkout)
        git("config", "user.email", "test@example.invalid", cwd=checkout)
        git("checkout", "-b", "side", cwd=checkout)
        git("commit", "--allow-empty", "-m", "side", cwd=checkout)
        git("checkout", "pingpong/requests", cwd=checkout)
        git("merge", "--no-ff", "side", "-m", "merge", cwd=checkout)
        git("push", "origin", "pingpong/requests", cwd=checkout)
        result = self.call("serve", "--once", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("merge commits are not supported", result.stderr)

    def test_two_concurrent_results_preserve_each_other_and_their_outputs(self) -> None:
        for ident in ("result-one", "result-two"):
            self.submit(self.request(ident, "pass"))
        settings = bridge.load_settings(self.config)
        first, second = bridge.ConcurrentGitTransport(settings), bridge.ConcurrentGitTransport(settings)
        connection = bridge.connect_database(self.state / "state.sqlite3")
        try:
            bridge.ingest_requests(connection, first, settings)
            rows = [connection.execute("SELECT * FROM jobs WHERE id = ?", (bridge.group_job_id("project", ident),)).fetchone()
                    for ident in ("result-one", "result-two")]
        finally:
            connection.close()
        results = []
        for row in rows:
            folder = self.state / "commands" / row["id"]
            folder.mkdir(parents=True)
            (folder / "stdout.snapshot").write_bytes(row["id"].encode())
            (folder / "stderr.snapshot").write_bytes(b"")
            result = bridge.make_result(row, "completed", "done")
            result["command_result"] = {
                "stdout": bridge.command_output_info(folder / "stdout.snapshot", row["id"], "stdout"),
                "stderr": bridge.command_output_info(folder / "stderr.snapshot", row["id"], "stderr"),
                "exit_code": 0, "timed_out": False, "output_complete": True}
            results.append(result)
        base = first.branch_tip(settings.results_branch)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        raced, conflicts = set(), []
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
                futures = [pool.submit(transport.publish_command_result, row, result, base, False, "project")
                           for transport, row, result in zip((first, second), rows, results)]
                for future in futures:
                    future.result(timeout=25)
        self.assertTrue(conflicts)
        group = json.loads(git("--git-dir", str(self.remote), "show",
                               "refs/heads/pingpong/results:pingpong/task-results/project.json"))
        self.assertEqual(set(group["results"]), {"result-one", "result-two"})
        for row in rows:
            logical_id = json.loads(row["request_json"])["id"]
            info = group["results"][logical_id]["command_result"]["stdout"]
            self.assertEqual(info["sha256"], hashlib.sha256(row["id"].encode()).hexdigest())
            self.assertEqual(git("--git-dir", str(self.remote), "show",
                                 "refs/heads/pingpong/results:" + info["parts"][0]["path"]), row["id"])

    def test_large_task_file_can_be_ingested_without_executing_its_commands(self) -> None:
        settings = bridge.load_settings(self.config)
        transport = bridge.ConcurrentGitTransport(settings)
        requests = [self.request("big-%03d" % i, "#" + ("x" * 14000)) for i in range(100)]
        content = bridge.json_bytes({"version": 2, "task_id": "large", "commands": requests})
        self.assertGreater(len(content), 1024 * 1024 + bridge.MAX_REQUEST_BYTES)
        transport.publish(settings.requests_branch, bridge.GROUP_REQUEST_PREFIX + "large.json", content,
                          immutable=True, confirm_remote=False)
        connection = bridge.connect_database(self.state / "state.sqlite3")
        try:
            bridge.ingest_requests(connection, transport, settings)
            count = connection.execute("SELECT count(*) FROM task_groups WHERE group_id='large'").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 100)

    @unittest.skipIf(os.name == "nt", "the size-rejecting hook uses a POSIX shell")
    def test_large_grouped_result_recovers_without_reexecuting(self) -> None:
        marker = self.root / "group-recovery-calls"
        rejected_once = self.root / "rejected-once"
        source = ("import os; from pathlib import Path; p=Path(" + repr(str(marker)) + "); "
                  "p.write_bytes((p.read_bytes() if p.exists() else b'')+b'X'); "
                  "os.write(1,bytes(range(256))*800)")
        self.submit(self.request("large-output", source))
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            "#!/bin/sh\nread old new ref\n"
            "for path in $(git diff-tree -r --no-commit-id --name-only \"$old\" \"$new\" -- pingpong/outputs/); do\n"
            "  size=$(git cat-file -s \"$new:$path\") || exit 1\n"
            "  if [ \"$size\" -gt 70000 ]; then exit 1; fi\n"
            "  case \"$path\" in *.000001) if [ ! -e " + shlex.quote(str(rejected_once)) +
            " ]; then : > " + shlex.quote(str(rejected_once)) + "; exit 1; fi ;; esac\n"
            "done\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        settings = bridge.load_settings(self.config)
        transport = bridge.ConcurrentGitTransport(settings)
        internal_id = bridge.group_job_id("project", "large-output")
        connection = bridge.connect_database(self.state / "state.sqlite3")
        try:
            bridge.ingest_requests(connection, transport, settings)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (internal_id,)).fetchone()
            bridge.update_job(connection, internal_id, "accepted", bridge.make_result(row, "accepted", "accepted"))
        finally:
            connection.close()
        self.assertTrue(bridge.task_worker(settings, internal_id))
        self.assertFalse(bridge.publication_worker(settings, internal_id))
        pending = json.loads(self.call("status", "large-output", "--task", "project").stdout)
        self.assertEqual(pending["state"], "delivery_pending")
        self.assertEqual(pending["execution_state"], "completed")
        self.assertEqual(pending["delivery_error"], "remote_rejected")
        self.assertTrue(bridge.publication_worker(settings, internal_id))
        result = json.loads(self.call("status", "large-output", "--task", "project").stdout)
        self.assertEqual(result["state"], "completed")
        self.assertGreater(len(result["command_result"]["stdout"]["parts"]), 1)
        output = self.root / "group-recovered.bin"
        self.call("output", "large-output", "--task", "project", "--stream", "stdout", "--output", str(output))
        self.assertEqual(output.read_bytes(), bytes(range(256)) * 800)
        self.assertEqual(marker.read_bytes(), b"X")


if __name__ == "__main__":
    unittest.main()
