#!/usr/bin/env python3
"""Git transport and a service for blue/green agent handoffs.

Agent tasks use the configured adapter. Explicit command tasks run locally and
publish their captured output as Git blobs without invoking the agent.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import email.utils
import hashlib
import http.client
import io
import json
import logging
import ntpath
import os
import posixpath
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LOGGER = logging.getLogger("green_bridge")
TASK_LOG_LEVEL = logging.INFO + 1
logging.addLevelName(TASK_LOG_LEVEL, "任务")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
COMMIT_ID = re.compile(r"^[0-9a-fA-F]{7,64}$")
ACTIONS = {
    "session.new", "session.attach", "session.switch", "session.checkpoint",
    "task.execute", "command.execute",
}
TERMINAL_STATES = {"completed", "failed", "needs_input", "unknown"}
MAX_REQUEST_BYTES = 262144
MAX_AGENT_RESPONSE_BYTES = 1048576
MAX_GROUP_BYTES = 16 * 1024 * 1024
COMMAND_CHUNK_BYTES = 4 * 1024 * 1024
RECOVERY_CHUNK_BYTES = 64 * 1024
MAX_RECOVERY_OUTPUT_BYTES = 8 * 1024 * 1024
INLINE_OUTPUT_BYTES = 4096
MAX_API_COMMIT_BODY_BYTES = 96000
GCM_NO_STORE_HELPER = '!f() { case "$1" in get|erase) git credential-manager "$1" ;; store) : ;; esac; }; f'
REQUEST_PREFIX = "pingpong/requests/"
RESULT_PREFIX = "pingpong/results/"
OUTPUT_PREFIX = "pingpong/outputs/"
GROUP_REQUEST_PREFIX = "pingpong/task-requests/"
GROUP_RESULT_PREFIX = "pingpong/task-results/"


class RepeatedWarningFilter(logging.Filter):
    """Keep recurring identical failures visible without logging every retry."""

    def __init__(self) -> None:
        super().__init__()
        self.last_emitted: Dict[Tuple[int, str], float] = {}
        self.lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        message = (record.levelno, record.getMessage())
        now = time.monotonic()
        with self.lock:
            previous = self.last_emitted.get(message)
            if previous is not None and now - previous < 3600:
                return False
            if previous is None and len(self.last_emitted) >= 1024:
                del self.last_emitted[next(iter(self.last_emitted))]
            self.last_emitted[message] = now
        return True


class BridgeError(RuntimeError):
    """A configuration, transport, or protocol error requiring attention."""


class WatchRateLimit(BridgeError):
    """A GitHub rate limit requires the watcher to pause."""

    def __init__(self, retry_seconds: int) -> None:
        super().__init__("GitHub watch is rate limited")
        self.retry_seconds = retry_seconds


class ApiPublicationError(BridgeError):
    """An atomic API result could not be safely confirmed."""


class ApiRateLimit(ApiPublicationError):
    """A GitHub API limit requires a shared pause before another API attempt."""

    def __init__(self, retry_seconds: int) -> None:
        super().__init__("atomic API publication is rate limited")
        self.retry_seconds = retry_seconds


class ApiHeadConflict(ApiPublicationError):
    """The branch advanced before an exact-head API update."""


class ApiPayloadTooLarge(ApiPublicationError):
    """A known oversized result must use the Git publication path."""


class GitOperationError(BridgeError):
    """A Git failure classified without including credentials or remote output."""

    def __init__(self, operation: str, kind: str) -> None:
        super().__init__("Git %s failed (%s)" % (operation, kind))
        self.kind = kind


class PushError(BridgeError):
    """A Git push failed or its outcome could not be confirmed."""

    def __init__(self, base: str, kind: str = "unknown") -> None:
        super().__init__("Git push failed (%s)" % kind)
        self.base = base
        self.kind = kind


@dataclass(frozen=True)
class Settings:
    git_dir: Path
    state_dir: Path
    git_executable: str
    git_config_global: Optional[Path]
    powershell_executable: str
    remote: str
    requests_branch: str
    results_branch: str
    adapter_argv: Tuple[str, ...]
    poll_seconds: int
    default_timeout_seconds: int
    max_timeout_seconds: int
    start_after: str
    author_name: str
    author_email: str
    watch_mode: str
    reconcile_seconds: int
    result_confirmation: str
    concurrent_tasks: bool
    command_workers: int
    publish_workers: int
    background_sync: bool
    api_results: bool
    gcm_skip_store: bool
    api_submit: bool = False


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def parse_json(data: bytes, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("%s is not a UTF-8 JSON object" % label) from exc
    if not isinstance(value, dict):
        raise BridgeError("%s must be a JSON object" % label)
    return value


def classify_git_error(stderr: bytes) -> str:
    """Return only a fixed category; Git's raw errors can contain private data."""
    error = stderr.decode("utf-8", errors="replace").lower()
    http = re.search(r"(?:http\s+|returned error:\s*)([45][0-9]{2})\b", error)
    if http:
        return "http_" + http.group(1)
    if "authentication failed" in error or "could not read username" in error:
        return "authentication"
    if "non-fast-forward" in error or "fetch first" in error:
        return "branch_advanced"
    if "remote rejected" in error or "hook declined" in error:
        return "remote_rejected"
    if "connection reset" in error or "hung up unexpectedly" in error or "broken pipe" in error:
        return "connection_closed"
    if "timed out" in error:
        return "timeout"
    if "proxy" in error:
        return "proxy"
    return "other"


def setting_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BridgeError("%s must be an integer in [%d, %d]" % (name, minimum, maximum))
    return value


def absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BridgeError("%s must be an absolute path" % name)
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise BridgeError("%s must be an absolute path" % name)
    return path.resolve()


def load_settings(path: Path) -> Settings:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BridgeError("cannot read configuration") from exc
    config = parse_json(data, "configuration")
    git_dir = absolute_path(config.get("git_dir"), "git_dir")
    state_dir = absolute_path(config.get("state_dir"), "state_dir")
    if git_dir == state_dir or git_dir in state_dir.parents or state_dir in git_dir.parents:
        raise BridgeError("git_dir and state_dir must be separate directories")
    git_executable = config.get("git_executable", "git")
    if not isinstance(git_executable, str) or not git_executable or git_executable.startswith("-"):
        raise BridgeError("git_executable must be a program name or absolute path")
    if git_executable != "git" and not Path(git_executable).is_absolute():
        raise BridgeError("git_executable must be git or an absolute path")
    git_config_value = config.get("git_config_global")
    git_config_global = None if git_config_value is None else absolute_path(git_config_value, "git_config_global")
    if git_config_global is not None and not git_config_global.is_file():
        raise BridgeError("git_config_global must point to an existing local file")
    powershell = config.get("powershell_executable", "powershell.exe" if os.name == "nt" else "pwsh")
    if not isinstance(powershell, str) or not powershell or powershell.startswith("-"):
        raise BridgeError("powershell_executable must be a program name or absolute path")
    remote = config.get("remote", "origin")
    if not isinstance(remote, str) or not REMOTE_NAME.fullmatch(remote):
        raise BridgeError("remote must be a configured Git remote name")
    requests_branch = config.get("requests_branch", "pingpong/requests")
    results_branch = config.get("results_branch", "pingpong/results")
    if not all(isinstance(v, str) and v and not v.startswith("-") for v in (requests_branch, results_branch)):
        raise BridgeError("branch names must be nonempty strings")
    if requests_branch == results_branch:
        raise BridgeError("requests and results branches must differ")
    adapter = config.get("adapter_argv", [])
    if not isinstance(adapter, list) or not all(isinstance(v, str) and v for v in adapter):
        raise BridgeError("adapter_argv must be a list of nonempty strings")
    poll = setting_int(config.get("poll_seconds", 60), "poll_seconds", 1, 86400)
    watch_mode = config.get("watch_mode", "git")
    if watch_mode not in ("git", "github_api"):
        raise BridgeError("watch_mode must be git or github_api")
    reconcile = setting_int(config.get("reconcile_seconds", 60), "reconcile_seconds", 5, 86400)
    result_confirmation = config.get("result_confirmation", "fetch")
    if result_confirmation not in ("fetch", "push_ack"):
        raise BridgeError("result_confirmation must be fetch or push_ack")
    concurrent_tasks = config.get("concurrent_tasks", False)
    if not isinstance(concurrent_tasks, bool):
        raise BridgeError("concurrent_tasks must be a boolean")
    background_sync = config.get("background_sync", False)
    if not isinstance(background_sync, bool):
        raise BridgeError("background_sync must be a boolean")
    api_results = config.get("api_results", False)
    if not isinstance(api_results, bool):
        raise BridgeError("api_results must be a boolean")
    if api_results and (not background_sync or not concurrent_tasks):
        raise BridgeError("api_results requires background_sync and concurrent_tasks")
    gcm_skip_store = config.get("gcm_skip_store", False)
    if not isinstance(gcm_skip_store, bool):
        raise BridgeError("gcm_skip_store must be a boolean")
    api_submit = config.get("api_submit", False)
    if not isinstance(api_submit, bool):
        raise BridgeError("api_submit must be a boolean")
    command_workers = setting_int(config.get("command_workers", 4), "command_workers", 1, 32)
    publish_workers = setting_int(config.get("publish_workers", 4), "publish_workers", 1, 32)
    default_timeout = setting_int(config.get("default_timeout_seconds", 1800), "default_timeout_seconds", 1, 604800)
    max_timeout = setting_int(config.get("max_timeout_seconds", 14400), "max_timeout_seconds", 1, 604800)
    if default_timeout > max_timeout:
        raise BridgeError("default timeout exceeds maximum timeout")
    start_after = config.get("start_after", "HEAD")
    if not isinstance(start_after, str) or (start_after != "HEAD" and not COMMIT_ID.fullmatch(start_after)):
        raise BridgeError("start_after must be HEAD or a commit ID")
    author_name = config.get("author_name", "Pingpong")
    author_email = config.get("author_email", "pingpong@localhost")
    if not all(isinstance(v, str) and v and "\n" not in v for v in (author_name, author_email)):
        raise BridgeError("author identity must be nonempty single-line strings")
    return Settings(git_dir, state_dir, git_executable, git_config_global, powershell, remote, requests_branch, results_branch, tuple(adapter),
                    poll, default_timeout, max_timeout, start_after, author_name, author_email,
                    watch_mode, reconcile, result_confirmation, concurrent_tasks,
                    command_workers, publish_workers, background_sync, api_results, gcm_skip_store, api_submit)


def validate_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise BridgeError("%s must use lowercase letters, digits, dot, underscore or hyphen" % name)
    return value


def group_job_id(group_id: str, command_id: str) -> str:
    """Give each task-local command a stable, globally unique internal ID."""
    group_id = validate_identifier(group_id, "task ID")
    command_id = validate_identifier(command_id, "command ID")
    return "g-" + hashlib.sha256((group_id + "\0" + command_id).encode("utf-8")).hexdigest()


def validate_request(data: bytes, expected_id: Optional[str] = None) -> Dict[str, Any]:
    if len(data) > MAX_REQUEST_BYTES:
        raise BridgeError("request exceeds size limit")
    request = parse_json(data, "request")
    if type(request.get("version")) is not int or request["version"] != 1:
        raise BridgeError("unsupported request version")
    task_id = validate_identifier(request.get("id"), "id")
    if expected_id is not None and task_id != expected_id:
        raise BridgeError("request ID does not match its filename")
    if request.get("action") not in ACTIONS:
        raise BridgeError("unsupported action")
    session = request.get("session")
    if session is not None:
        validate_identifier(session, "session")
    if request["action"] in {"session.new", "session.attach", "session.switch"} and session is None:
        raise BridgeError("this action requires a session alias")
    if request["action"] == "session.attach":
        native = request.get("native_session_id")
        if not isinstance(native, str) or not native or len(native) > 512:
            raise BridgeError("session.attach requires native_session_id")
    message = request.get("message", "")
    if not isinstance(message, str) or len(message) > 100000:
        raise BridgeError("message must be a string of at most 100000 characters")
    if request["action"] in {"task.execute", "session.checkpoint"} and not message.strip():
        raise BridgeError("this action requires a nonempty message")
    if request["action"] == "command.execute":
        command = request.get("command")
        if not isinstance(command, dict):
            raise BridgeError("command.execute requires a command object")
        cwd = command.get("cwd")
        if not isinstance(cwd, str) or not cwd or "\x00" in cwd or not (ntpath.isabs(cwd) or posixpath.isabs(cwd)):
            raise BridgeError("command cwd must be an absolute path")
        if not isinstance(command.get("report_running", True), bool):
            raise BridgeError("command report_running must be a boolean")
        mode = command.get("mode")
        if mode == "powershell":
            script = command.get("script")
            if not isinstance(script, str) or not script.strip() or "\x00" in script:
                raise BridgeError("powershell command requires a nonempty script")
        elif mode == "argv":
            argv = command.get("argv")
            if not isinstance(argv, list) or not argv or len(argv) > 256 or not all(
                isinstance(value, str) and "\x00" not in value for value in argv
            ) or not argv[0]:
                raise BridgeError("argv command requires a nonempty list of arguments")
        else:
            raise BridgeError("command mode must be powershell or argv")
    context = request.get("context", {})
    if not isinstance(context, dict):
        raise BridgeError("context must be an object")
    if "timeout_seconds" in request:
        setting_int(request["timeout_seconds"], "timeout_seconds", 1, 604800)
    return request


def validate_request_group(data: bytes, group_id: str) -> Dict[str, Any]:
    """Validate an append-only task file while keeping each command independently identified."""
    if len(data) > MAX_GROUP_BYTES:
        raise BridgeError("task request file exceeds size limit")
    group = parse_json(data, "task request file")
    if set(group) != {"version", "task_id", "commands"} or group.get("version") != 2:
        raise BridgeError("invalid task request file")
    if group.get("task_id") != validate_identifier(group_id, "task ID"):
        raise BridgeError("task ID does not match its filename")
    commands = group.get("commands")
    if not isinstance(commands, list):
        raise BridgeError("task commands must be a list")
    seen = set()
    for request in commands:
        if not isinstance(request, dict):
            raise BridgeError("task command must be an object")
        validated = validate_request(json_bytes(request))
        if validated["id"] in seen:
            raise BridgeError("duplicate command ID in task file")
        seen.add(validated["id"])
    return group


def append_group_bytes(old: Optional[bytes], group_id: str, requests: Sequence[Dict[str, Any]]) -> Optional[bytes]:
    """Append new commands without changing or duplicating existing commands."""
    group_id = validate_identifier(group_id, "task ID")
    if not requests:
        raise BridgeError("task batch must contain at least one command")
    group = validate_request_group(old, group_id) if old is not None else {
        "version": 2, "task_id": group_id, "commands": []}
    existing = {request["id"]: request for request in group["commands"]}
    changed = False
    for request in requests:
        validate_request(json_bytes(request))
        previous = existing.get(request["id"])
        if previous is not None:
            if previous != request:
                raise BridgeError("command ID already exists with different content")
            continue
        group["commands"].append(request)
        existing[request["id"]] = request
        changed = True
    if not changed:
        return None
    content = json_bytes(group)
    validate_request_group(content, group_id)
    return content


class GitTransport:
    """Use a dedicated bare clone without checking out or rewriting a worktree."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if not settings.git_dir.is_dir():
            raise BridgeError("git_dir does not exist")
        if self.run(["rev-parse", "--is-bare-repository"]).strip() != b"true":
            raise BridgeError("git_dir must be a dedicated bare Git clone")
        self.run(["check-ref-format", "--branch", settings.requests_branch])
        self.run(["check-ref-format", "--branch", settings.results_branch])

    def environment(self, env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        environment = os.environ.copy() if env is None else env.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GCM_INTERACTIVE"] = "Never"
        if self.settings.git_config_global is not None:
            environment["GIT_CONFIG_GLOBAL"] = str(self.settings.git_config_global)
        return environment

    def run(self, arguments: Sequence[str], data: Optional[bytes] = None,
            env: Optional[Dict[str, str]] = None, check: bool = True) -> bytes:
        command = [self.settings.git_executable]
        if self.settings.gcm_skip_store and arguments[0] in ("fetch", "push", "credential"):
            command.extend(["-c", "credential.helper=", "-c", "credential.helper=" + GCM_NO_STORE_HELPER])
        command.extend(["--git-dir", str(self.settings.git_dir), *arguments])
        environment = self.environment(env)
        try:
            process = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=environment, timeout=60, check=False)
        except subprocess.TimeoutExpired as exc:
            raise GitOperationError(arguments[0], "timeout") from exc
        except OSError as exc:
            raise GitOperationError(arguments[0], "start_failed") from exc
        if check and process.returncode != 0:
            error = process.stderr.decode("utf-8", errors="replace").lower()
            if "bad boolean config value" in error and "http.sslverify" in error:
                raise BridgeError("Git configuration http.sslVerify is not a valid boolean")
            raise GitOperationError(arguments[0], classify_git_error(process.stderr))
        return process.stdout

    def branch_tip(self, branch: str) -> str:
        ref = "refs/pingpong-read/" + uuid.uuid4().hex
        try:
            self.run(["fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                      self.settings.remote, "+refs/heads/" + branch + ":" + ref])
            return self.run(["rev-parse", "--verify", ref + "^{commit}"]).decode("ascii").strip()
        finally:
            try:
                self.run(["update-ref", "-d", ref], check=False)
            except BridgeError:
                LOGGER.warning("Could not remove temporary Git read reference")

    def branch_tips(self, branches: Sequence[str]) -> List[str]:
        """Fetch both service branches in one network operation into local cache refs."""
        refs = ["refs/pingpong-cache/" + hashlib.sha256(branch.encode("utf-8")).hexdigest() for branch in branches]
        specifications = ["+refs/heads/" + branch + ":" + ref for branch, ref in zip(branches, refs)]
        self.run(["fetch", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                  self.settings.remote, *specifications])
        return [self.run(["rev-parse", "--verify", ref + "^{commit}"]).decode("ascii").strip() for ref in refs]

    def remote_fingerprint(self) -> str:
        url = self.run(["remote", "get-url", self.settings.remote]).strip()
        return hashlib.sha256(url).hexdigest()

    def commit(self, value: str) -> str:
        if not COMMIT_ID.fullmatch(value):
            raise BridgeError("invalid commit ID")
        return self.run(["rev-parse", "--verify", value + "^{commit}"]).decode("ascii").strip()

    def is_ancestor(self, older: str, newer: str) -> bool:
        command = [self.settings.git_executable, "--git-dir", str(self.settings.git_dir),
                   "merge-base", "--is-ancestor", older, newer]
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=self.environment(), timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError("Git ancestry check could not complete") from exc
        if result.returncode not in (0, 1):
            raise BridgeError("Git ancestry check failed")
        return result.returncode == 0

    def blob(self, commit: str, path: str, limit: int = MAX_AGENT_RESPONSE_BYTES + MAX_REQUEST_BYTES) -> Optional[bytes]:
        command = [self.settings.git_executable, "--git-dir", str(self.settings.git_dir),
                   "cat-file", "-e", commit + ":" + path]
        try:
            check = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   env=self.environment(), timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BridgeError("Git path lookup could not complete") from exc
        if check.returncode != 0:
            return None
        raw_size = self.run(["cat-file", "-s", commit + ":" + path]).strip()
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise BridgeError("Git returned an invalid blob size") from exc
        if size > limit:
            raise BridgeError("Git blob exceeds the configured protocol limit")
        return self.run(["show", commit + ":" + path])

    def push_commit(self, branch: str, base: str, commit: str) -> None:
        try:
            self.run(["push", self.settings.remote, commit + ":refs/heads/" + branch])
        except BridgeError as exc:
            raise PushError(base, exc.kind if isinstance(exc, GitOperationError) else "unknown") from exc

    def publish(self, branch: str, path: str, content: bytes, immutable: bool = False,
                request_hash: Optional[str] = None, base_hint: Optional[str] = None,
                confirm_remote: bool = True) -> str:
        base = self.commit(base_hint) if base_hint is not None else self.branch_tip(branch)
        limit = MAX_GROUP_BYTES if path.startswith((GROUP_REQUEST_PREFIX, GROUP_RESULT_PREFIX)) else (
            MAX_AGENT_RESPONSE_BYTES + MAX_REQUEST_BYTES)
        old = self.blob(base, path, limit)
        if old == content:
            return base
        if old is not None and immutable:
            raise BridgeError("task ID already exists with different content")
        if old is not None and request_hash is not None:
            previous = parse_json(old, "existing result")
            if previous.get("request_sha256") != request_hash:
                raise BridgeError("result path belongs to a different request")
            if previous.get("state") in TERMINAL_STATES:
                raise BridgeError("terminal result cannot be replaced")
        with tempfile.TemporaryDirectory(prefix="pingpong-index-") as tmp:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            env["GIT_AUTHOR_NAME"] = self.settings.author_name
            env["GIT_AUTHOR_EMAIL"] = self.settings.author_email
            env["GIT_COMMITTER_NAME"] = self.settings.author_name
            env["GIT_COMMITTER_EMAIL"] = self.settings.author_email
            self.run(["read-tree", base], env=env)
            blob = self.run(["hash-object", "-w", "--stdin"], data=content).decode("ascii").strip()
            self.run(["update-index", "--add", "--cacheinfo", "100644", blob, path], env=env)
            tree = self.run(["write-tree"], env=env).decode("ascii").strip()
            commit = self.run(["commit-tree", tree, "-p", base, "-m", "pingpong: update " + path],
                              env=env).decode("ascii").strip()
        self.push_commit(branch, base, commit)
        if not confirm_remote:
            return commit
        confirmed = self.branch_tip(branch)
        if self.blob(confirmed, path, limit) != content:
            raise BridgeError("pushed content could not be confirmed on the remote")
        return confirmed

    def append_group_request(self, group_id: str, request: Dict[str, Any],
                             base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        return self.append_group_requests(group_id, [request], base_hint, confirm_remote)

    def append_group_requests(self, group_id: str, requests: Sequence[Dict[str, Any]],
                              base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        group_id = validate_identifier(group_id, "task ID")
        if not requests:
            raise BridgeError("task batch must contain at least one command")
        branch = self.settings.requests_branch
        base = self.commit(base_hint) if base_hint is not None else self.branch_tip(branch)
        path = GROUP_REQUEST_PREFIX + group_id + ".json"
        old = self.blob(base, path, MAX_GROUP_BYTES)
        content = append_group_bytes(old, group_id, requests)
        if content is None:
            return base
        commit = GitTransport.publish(self, branch, path, content, base_hint=base, confirm_remote=False)
        if not confirm_remote:
            return commit
        confirmed = self.branch_tip(branch)
        current = self.blob(confirmed, path, MAX_GROUP_BYTES)
        if current is None:
            raise BridgeError("pushed task command could not be confirmed on the remote")
        confirmed_commands = {value["id"]: value for value in validate_request_group(current, group_id)["commands"]}
        if any(confirmed_commands.get(request["id"]) != request for request in requests):
            raise BridgeError("pushed task command could not be confirmed on the remote")
        return confirmed

    def group_result_bytes(self, base: str, group_id: str, job: sqlite3.Row,
                           result: Dict[str, Any]) -> bytes:
        return self.group_results_bytes(base, group_id, [(job, result)])

    def group_results_bytes(self, base: str, group_id: str,
                            entries: Sequence[Tuple[sqlite3.Row, Dict[str, Any]]]) -> bytes:
        group_id = validate_identifier(group_id, "task ID")
        path = GROUP_RESULT_PREFIX + group_id + ".json"
        old = self.blob(base, path, MAX_GROUP_BYTES)
        group = parse_json(old, "task result file") if old is not None else {
            "version": 2, "task_id": group_id, "results": {}}
        if (set(group) != {"version", "task_id", "results"} or group.get("version") != 2 or
                group.get("task_id") != group_id or not isinstance(group.get("results"), dict)):
            raise BridgeError("invalid task result file")
        for job, result in entries:
            command_id = validate_identifier(result.get("id"), "command ID")
            if command_id != json.loads(job["request_json"]).get("id"):
                raise BridgeError("task result command ID does not match its request")
            previous = group["results"].get(command_id)
            if previous is not None:
                if not isinstance(previous, dict) or previous.get("request_sha256") != job["request_hash"]:
                    raise BridgeError("task result belongs to a different request")
                if previous.get("state") in TERMINAL_STATES and previous != result:
                    raise BridgeError("terminal task result cannot be replaced")
            group["results"][command_id] = result
        content = json_bytes(group)
        if len(content) > MAX_GROUP_BYTES:
            raise BridgeError("task result file exceeds size limit")
        return content

    def publish_inline_group_results(self, group_id: str,
                                     entries: Sequence[Tuple[sqlite3.Row, Dict[str, Any]]],
                                     base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        """Publish several verified inline command results together in one commit."""
        if not entries:
            raise BridgeError("result batch must contain at least one command")
        verify_inline_group_entries(self.settings, entries)
        branch = self.settings.results_branch
        base = self.commit(base_hint) if base_hint is not None else self.branch_tip(branch)
        content = self.group_results_bytes(base, group_id, entries)
        path = GROUP_RESULT_PREFIX + group_id + ".json"
        commit = GitTransport.publish(self, branch, path, content, base_hint=base, confirm_remote=False)
        if not confirm_remote:
            return commit
        confirmed = self.branch_tip(branch)
        if any(read_result(self, confirmed, result["id"], group_id) != result for _, result in entries):
            raise BridgeError("pushed task results could not be confirmed on the remote")
        return confirmed

    def publish_group_state(self, group_id: str, job: sqlite3.Row, result: Dict[str, Any],
                            base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        branch = self.settings.results_branch
        base = self.commit(base_hint) if base_hint is not None else self.branch_tip(branch)
        content = self.group_result_bytes(base, group_id, job, result)
        path = GROUP_RESULT_PREFIX + group_id + ".json"
        commit = GitTransport.publish(self, branch, path, content, base_hint=base, confirm_remote=False)
        if not confirm_remote:
            return commit
        confirmed = self.branch_tip(branch)
        if read_result(self, confirmed, result["id"], group_id) != result:
            raise BridgeError("pushed task result could not be confirmed on the remote")
        return confirmed

    def publish_command_result(self, job: sqlite3.Row, result: Dict[str, Any],
                               base_hint: Optional[str] = None, confirm_remote: bool = True,
                               group_id: Optional[str] = None) -> str:
        """Publish final metadata and immutable raw-output parts in one commit."""
        branch = self.settings.results_branch
        base = self.commit(base_hint) if base_hint is not None else self.branch_tip(branch)
        result_path = (GROUP_RESULT_PREFIX + group_id + ".json" if group_id is not None else
                       RESULT_PREFIX + job["id"] + ".json")
        if group_id is not None:
            result_data = self.group_result_bytes(base, group_id, job, result)
        else:
            result_data = json_bytes(result)
            previous_data = self.blob(base, result_path)
            if previous_data is not None:
                previous = parse_json(previous_data, "existing result")
                if previous.get("request_sha256") != job["request_hash"]:
                    raise BridgeError("result path belongs to a different request")
                if previous.get("state") in TERMINAL_STATES and previous != result:
                    raise BridgeError("terminal result cannot be replaced")
        with tempfile.TemporaryDirectory(prefix="pingpong-index-") as tmp:
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = str(Path(tmp) / "index")
            env["GIT_AUTHOR_NAME"] = self.settings.author_name
            env["GIT_AUTHOR_EMAIL"] = self.settings.author_email
            env["GIT_COMMITTER_NAME"] = self.settings.author_name
            env["GIT_COMMITTER_EMAIL"] = self.settings.author_email
            self.run(["read-tree", base], env=env)
            expected_blobs = {}
            for stream in ("stdout", "stderr"):
                snapshot = self.settings.state_dir / "commands" / job["id"] / (stream + ".snapshot")
                with snapshot.open("rb") as source:
                    info = result["command_result"][stream]
                    inline = info.get("inline_base64")
                    if inline is not None:
                        data = decode_inline_output(info)
                        if source.read(INLINE_OUTPUT_BYTES + 1) != data:
                            raise BridgeError("local command output changed before publication")
                        continue
                    for part in info["parts"]:
                        data = source.read(part["size"])
                        if len(data) != part["size"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
                            raise BridgeError("local command output changed before publication")
                        oid = self.run(["hash-object", "-w", "--stdin"], data=data).decode("ascii").strip()
                        old_oid = self.run(["rev-parse", "--verify", base + ":" + part["path"]],
                                           check=False).decode("ascii").strip()
                        if old_oid and old_oid != oid:
                            raise BridgeError("command output path already exists with different content")
                        self.run(["update-index", "--add", "--cacheinfo", "100644", oid, part["path"]], env=env)
                        expected_blobs[part["path"]] = oid
                    if source.read(1):
                        raise BridgeError("local command output changed before publication")
            result_oid = self.run(["hash-object", "-w", "--stdin"], data=result_data).decode("ascii").strip()
            self.run(["update-index", "--add", "--cacheinfo", "100644", result_oid, result_path], env=env)
            tree = self.run(["write-tree"], env=env).decode("ascii").strip()
            if tree == self.run(["rev-parse", base + "^{tree}"]).decode("ascii").strip():
                return base
            commit = self.run(["commit-tree", tree, "-p", base, "-m", "pingpong: command " + job["id"]],
                              env=env).decode("ascii").strip()
        started = time.monotonic()
        self.push_commit(branch, base, commit)
        LOGGER.info("Command result push for %s took %.3f seconds", job["id"], time.monotonic() - started)
        if not confirm_remote:
            return commit
        started = time.monotonic()
        confirmed = self.branch_tip(branch)
        LOGGER.info("Command result confirmation for %s took %.3f seconds", job["id"], time.monotonic() - started)
        if group_id is not None:
            if read_result(self, confirmed, result["id"], group_id) != result:
                raise BridgeError("pushed task result could not be confirmed on the remote")
        elif self.blob(confirmed, result_path) != result_data:
            raise BridgeError("pushed result could not be confirmed on the remote")
        for path, oid in expected_blobs.items():
            actual = self.run(["rev-parse", "--verify", confirmed + ":" + path]).decode("ascii").strip()
            if actual != oid:
                raise BridgeError("pushed command output could not be confirmed on the remote")
        return confirmed

    def changed_paths(self, previous: str, head: str, prefix: str) -> List[Tuple[str, Optional[str], str, str]]:
        if not self.is_ancestor(previous, head):
            raise BridgeError("request branch was rewritten; stop and inspect it")
        revision = previous + ".." + head
        lines = self.run(["rev-list", "--reverse", "--first-parent", "--parents", revision]).decode("ascii").splitlines()
        parents = {}
        for line in lines:
            values = line.split()
            if len(values) > 2:
                raise BridgeError("merge commits are not supported on the request branch")
            parents[values[0]] = values[1] if len(values) == 2 else None
        raw = self.run(["log", "--reverse", "--first-parent", "--format=pingpong-commit:%H", "--name-status",
                        "--no-renames", revision, "--", prefix]).decode("utf-8")
        changes = []
        commit = ""
        for line in raw.splitlines():
            if line.startswith("pingpong-commit:"):
                commit = line[len("pingpong-commit:"):]
                if commit not in parents:
                    raise BridgeError("Git returned an unexpected request commit")
                continue
            status, separator, path = line.partition("\t")
            if separator and path.endswith(".json"):
                if not commit:
                    raise BridgeError("Git returned a request change without a commit")
                changes.append((commit, parents[commit], status, path))
        return changes

    def batch_blobs(self, specs: Sequence[Tuple[str, str]], limit: int) -> List[Optional[bytes]]:
        if not specs:
            return []
        query = "".join(commit + ":" + path + "\n" for commit, path in specs).encode("utf-8")
        headers = self.run(["cat-file", "--batch-check"], data=query).splitlines()
        if len(headers) != len(specs):
            raise BridgeError("Git returned an incomplete batch size listing")
        for raw in headers:
            header = raw.split()
            if len(header) == 2 and header[1] == b"missing":
                continue
            if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
                raise BridgeError("Git returned an invalid batch blob header")
            if int(header[2]) > limit:
                raise BridgeError("Git blob exceeds the configured protocol limit")
        output = io.BytesIO(self.run(["cat-file", "--batch"], data=query))
        blobs = []
        for _ in specs:
            header = output.readline().split()
            if len(header) == 2 and header[1] == b"missing":
                blobs.append(None)
                continue
            if len(header) != 3 or header[1] != b"blob" or not header[2].isdigit():
                raise BridgeError("Git returned an invalid batch blob header")
            size = int(header[2])
            if size > limit:
                raise BridgeError("Git blob exceeds the configured protocol limit")
            content = output.read(size)
            if len(content) != size or output.read(1) != b"\n":
                raise BridgeError("Git returned an incomplete batch blob")
            blobs.append(content)
        return blobs

    def added_requests(self, previous: str, head: str) -> List[Tuple[str, str, bytes]]:
        changes = self.changed_paths(previous, head, REQUEST_PREFIX)
        requests = []
        for start in range(0, len(changes), 4):
            batch = changes[start:start + 4]
            for _commit, _parent, status, _path in batch:
                if status != "A":
                    raise BridgeError("request files must be append-only")
            blobs = self.batch_blobs([(commit, path) for commit, _parent, _status, path in batch],
                                     MAX_AGENT_RESPONSE_BYTES + MAX_REQUEST_BYTES)
            for (commit, _parent, _status, path), content in zip(batch, blobs):
                task_id = validate_identifier(path[len(REQUEST_PREFIX):-5], "request filename")
                if content is None:
                    raise BridgeError("added request is missing")
                requests.append((commit, task_id, content))
        return requests

    def added_group_requests(self, previous: str, head: str) -> List[Tuple[str, str, bytes, str]]:
        changes = self.changed_paths(previous, head, GROUP_REQUEST_PREFIX)
        requests = []
        for start in range(0, len(changes), 2):
            batch = changes[start:start + 2]
            specs = []
            for commit, parent, status, path in batch:
                if status not in {"A", "M"}:
                    raise BridgeError("task request files cannot be removed or renamed")
                if status == "M" and parent is None:
                    raise BridgeError("task request modification has no parent")
                specs.append((commit, path))
                if status == "M":
                    specs.append((parent, path))
            contents = iter(self.batch_blobs(specs, MAX_GROUP_BYTES))
            for commit, _parent, status, path in batch:
                group_id = validate_identifier(path[len(GROUP_REQUEST_PREFIX):-5], "task filename")
                content = next(contents)
                if content is None:
                    raise BridgeError("task request file is missing")
                current = validate_request_group(content, group_id)
                prior_commands = []
                if status == "M":
                    old = next(contents)
                    if old is None:
                        raise BridgeError("previous task request file is missing")
                    prior_commands = validate_request_group(old, group_id)["commands"]
                commands = current["commands"]
                if commands[:len(prior_commands)] != prior_commands:
                    raise BridgeError("existing task commands cannot be changed or removed")
                for request in commands[len(prior_commands):]:
                    requests.append((commit, request["id"], json_bytes(request), group_id))
        return requests


class ConcurrentGitTransport(GitTransport):
    """Rebuild only a publication when another writer advanced the branch."""

    def publish(self, branch: str, path: str, content: bytes, immutable: bool = False,
                request_hash: Optional[str] = None, base_hint: Optional[str] = None,
                confirm_remote: bool = True) -> str:
        base = base_hint
        for _ in range(8):
            try:
                return super().publish(branch, path, content, immutable, request_hash, base, confirm_remote)
            except PushError as exc:
                base = self.retry_base(branch, exc)
        raise BridgeError("Git branch changed too often during publication")

    def publish_command_result(self, job: sqlite3.Row, result: Dict[str, Any],
                               base_hint: Optional[str] = None, confirm_remote: bool = True,
                               group_id: Optional[str] = None) -> str:
        base = base_hint
        for _ in range(8):
            try:
                return super().publish_command_result(job, result, base, confirm_remote, group_id)
            except PushError as exc:
                base = self.retry_base(self.settings.results_branch, exc)
        raise BridgeError("Git branch changed too often during publication")

    def append_group_requests(self, group_id: str, requests: Sequence[Dict[str, Any]],
                              base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        base = base_hint
        for _ in range(8):
            try:
                return super().append_group_requests(group_id, requests, base, confirm_remote)
            except PushError as exc:
                base = self.retry_base(self.settings.requests_branch, exc)
        raise BridgeError("Git branch changed too often during task submission")

    def publish_group_state(self, group_id: str, job: sqlite3.Row, result: Dict[str, Any],
                            base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        base = base_hint
        for _ in range(8):
            try:
                return super().publish_group_state(group_id, job, result, base, confirm_remote)
            except PushError as exc:
                base = self.retry_base(self.settings.results_branch, exc)
        raise BridgeError("Git branch changed too often during task publication")

    def publish_inline_group_results(self, group_id: str,
                                     entries: Sequence[Tuple[sqlite3.Row, Dict[str, Any]]],
                                     base_hint: Optional[str] = None, confirm_remote: bool = True) -> str:
        base = base_hint
        for _ in range(8):
            try:
                return super().publish_inline_group_results(group_id, entries, base, confirm_remote)
            except PushError as exc:
                base = self.retry_base(self.settings.results_branch, exc)
        raise BridgeError("Git branch changed too often during batch publication")

    def retry_base(self, branch: str, failure: PushError) -> str:
        head = self.branch_tip(branch)
        if head == failure.base:
            # A competing receive-pack may still hold the remote ref lock.
            time.sleep(0.2)
            head = self.branch_tip(branch)
            if head == failure.base:
                raise failure
        if not self.is_ancestor(failure.base, head):
            raise BridgeError("Git branch was rewritten during publication") from failure
        return head


class GitHubRefWatcher:
    """Check one GitHub ref over a reusable, authenticated HTTPS connection."""

    def __init__(self, git: GitTransport, settings: Settings) -> None:
        remote = git.run(["remote", "get-url", settings.remote]).decode("utf-8").strip()
        parsed = urllib.parse.urlsplit(remote)
        match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or match is None:
            raise BridgeError("GitHub watch requires an HTTPS github.com repository remote")
        self.path = "/repos/%s/%s/branches/%s" % (
            match.group(1), match.group(2), urllib.parse.quote(settings.requests_branch, safe=""))
        self.branch = settings.requests_branch
        credential_input = ("protocol=https\nhost=github.com\npath=%s\n\n" %
                            parsed.path.lstrip("/").rstrip("/")).encode("utf-8")
        credential = git.run(["credential", "fill"], data=credential_input, check=False)
        password = next((line[9:] for line in credential.decode("utf-8").splitlines()
                         if line.startswith("password=")), "")
        if not password and parsed.password:
            password = urllib.parse.unquote(parsed.password)
        if not password:
            raise BridgeError("GitHub watch could not obtain a Git credential")
        self.authorization = "Bearer " + password
        raw_proxy = git.run(["config", "--get-urlmatch", "http.proxy", "https://github.com"],
                            check=False).decode("utf-8").strip()
        if not raw_proxy:
            raw_proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or ""
        self.proxy_host: Optional[str] = None
        self.proxy_port = 80
        self.proxy_headers: Dict[str, str] = {}
        if raw_proxy:
            if "://" not in raw_proxy:
                raw_proxy = "http://" + raw_proxy
            proxy = urllib.parse.urlsplit(raw_proxy)
            try:
                port = proxy.port
            except ValueError as exc:
                raise BridgeError("GitHub watch proxy has an invalid port") from exc
            if proxy.scheme != "http" or not proxy.hostname:
                raise BridgeError("GitHub watch supports HTTP proxies only")
            self.proxy_host = proxy.hostname
            self.proxy_port = port or 80
            if proxy.username is not None:
                username = urllib.parse.unquote(proxy.username)
                secret = urllib.parse.unquote(proxy.password or "")
                encoded = base64.b64encode((username + ":" + secret).encode("utf-8")).decode("ascii")
                self.proxy_headers["Proxy-Authorization"] = "Basic " + encoded
        self.connection: Optional[http.client.HTTPSConnection] = None
        self.etag: Optional[str] = None
        self.head: Optional[str] = None
        self.poll_interval = 0
        self.next_check_at = 0.0

    def check(self) -> str:
        headers = {"Authorization": self.authorization, "Accept": "application/vnd.github+json",
                   "User-Agent": "blue-green-pingpong"}
        if self.etag:
            headers["If-None-Match"] = self.etag
        for attempt in range(2):
            if self.connection is None:
                self.connection = http.client.HTTPSConnection(self.proxy_host or "api.github.com",
                                                              self.proxy_port if self.proxy_host else 443,
                                                              timeout=8)
                if self.proxy_host:
                    self.connection.set_tunnel("api.github.com", 443, headers=self.proxy_headers)
            try:
                self.connection.request("GET", self.path, headers=headers)
                response = self.connection.getresponse()
                body = response.read(MAX_REQUEST_BYTES + 1)
                break
            except (ConnectionResetError, BrokenPipeError) as exc:
                self.close()
                if attempt:
                    raise BridgeError("GitHub watch connection closed") from exc
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                raise BridgeError("GitHub watch request failed") from exc
        if len(body) > MAX_REQUEST_BYTES:
            self.close()
            raise BridgeError("GitHub watch response exceeds size limit")
        interval = response.getheader("X-Poll-Interval")
        self.poll_interval = int(interval) if interval and interval.isdecimal() else 0
        self.next_check_at = time.monotonic() + self.poll_interval
        if response.status == 304 and self.head:
            return self.head
        if response.status in (403, 429):
            retry = response.getheader("Retry-After")
            reset = response.getheader("X-RateLimit-Reset")
            if retry and retry.isdecimal():
                retry_seconds = int(retry)
            elif response.getheader("X-RateLimit-Remaining") == "0" and reset and reset.isdecimal():
                server_date = response.getheader("Date")
                try:
                    server_time = email.utils.parsedate_to_datetime(server_date).timestamp() if server_date else time.time()
                except (ValueError, TypeError, OverflowError):
                    server_time = time.time()
                retry_seconds = int(reset) - int(server_time) + 1
            else:
                retry_seconds = 60
            self.close()
            raise WatchRateLimit(max(60, retry_seconds))
        if response.status != 200:
            self.close()
            raise BridgeError("GitHub watch returned HTTP %d" % response.status)
        payload = parse_json(body, "GitHub watch response")
        commit_info = payload.get("commit")
        head = commit_info.get("sha") if isinstance(commit_info, dict) else None
        if (payload.get("name") != self.branch or not isinstance(head, str) or
                len(head) not in (40, 64) or not COMMIT_ID.fullmatch(head)):
            self.close()
            raise BridgeError("GitHub watch returned an invalid ref")
        self.etag = response.getheader("ETag")
        self.head = head
        return head

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def api_rate_limit_seconds(response: http.client.HTTPResponse) -> Optional[int]:
    retry = response.getheader("Retry-After")
    limited = response.status == 429 or (response.status in (200, 403) and
              (response.getheader("X-RateLimit-Remaining") == "0" or bool(retry)))
    if not limited:
        return None
    reset = response.getheader("X-RateLimit-Reset")
    if retry and retry.isdecimal():
        seconds = int(retry)
    elif reset and reset.isdecimal():
        server_date = response.getheader("Date")
        try:
            server_time = email.utils.parsedate_to_datetime(server_date).timestamp() if server_date else time.time()
        except (ValueError, TypeError, OverflowError):
            server_time = time.time()
        seconds = int(reset) - int(server_time) + 1
    else:
        seconds = 60
    return max(60, seconds)


class GitHubAtomicTransport:
    """Publish a bounded file with an exact expected branch head."""

    def __init__(self, git: GitTransport, settings: Settings) -> None:
        self.watcher = GitHubRefWatcher(git, settings)
        parts = self.watcher.path.split("/")
        self.repository = parts[2] + "/" + parts[3]
        self.connection = http.client.HTTPSConnection(
            self.watcher.proxy_host or "api.github.com",
            self.watcher.proxy_port if self.watcher.proxy_host else 443, timeout=8)
        if self.watcher.proxy_host:
            self.connection.set_tunnel("api.github.com", 443, headers=self.watcher.proxy_headers)

    def publish(self, branch: str, expected: str, path: str, content: bytes,
                headline: str = "pingpong: publish results") -> str:
        if len(expected) not in (40, 64) or not COMMIT_ID.fullmatch(expected):
            raise ApiPublicationError("API publication requires a full expected branch head")
        query = ("mutation($input:CreateCommitOnBranchInput!){"
                 "createCommitOnBranch(input:$input){commit{oid}}}")
        variables = {"input": {"branch": {"repositoryNameWithOwner": self.repository, "branchName": branch},
                               "expectedHeadOid": expected, "message": {"headline": headline},
                               "fileChanges": {"additions": [{"path": path,
                                   "contents": base64.b64encode(content).decode("ascii")}]}}}
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_API_COMMIT_BODY_BYTES:
            raise ApiPayloadTooLarge("API result exceeds the bounded request size")
        headers = {"Authorization": self.watcher.authorization, "Accept": "application/vnd.github+json",
                   "Content-Type": "application/json", "User-Agent": "blue-green-pingpong"}
        try:
            self.connection.request("POST", "/graphql", body=body, headers=headers)
            response = self.connection.getresponse()
            raw = response.read(MAX_AGENT_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise ApiPublicationError("atomic API publication could not be confirmed") from exc
        if len(raw) > MAX_AGENT_RESPONSE_BYTES:
            raise ApiPublicationError("atomic API response exceeds size limit")
        retry_seconds = api_rate_limit_seconds(response)
        if retry_seconds is not None:
            raise ApiRateLimit(retry_seconds)
        if response.status != 200:
            raise ApiPublicationError("atomic API publication returned HTTP %d" % response.status)
        try:
            payload = parse_json(raw, "atomic API response")
        except BridgeError as exc:
            raise ApiPublicationError("atomic API response is invalid") from exc
        data = payload.get("data")
        created = data.get("createCommitOnBranch") if isinstance(data, dict) else None
        value = created.get("commit") if isinstance(created, dict) else None
        oid = value.get("oid") if isinstance(value, dict) else None
        errors = payload.get("errors")
        if (oid is None and isinstance(errors, list) and errors and
                all(isinstance(error, dict) and error.get("type") == "STALE_DATA" for error in errors)):
            raise ApiHeadConflict("atomic API branch head changed")
        if payload.get("errors") or not isinstance(oid, str) or len(oid) not in (40, 64) or not COMMIT_ID.fullmatch(oid):
            raise ApiPublicationError("atomic API publication was rejected or could not be confirmed")
        return oid

    def close(self) -> None:
        self.connection.close()
        self.watcher.close()


class GitHubRequestTransport(GitHubAtomicTransport):
    """Read a task and compare all locally observed heads in one API snapshot."""

    def snapshot(self, branch: str, group_id: str, known_heads: Sequence[str]) -> Tuple[str, Optional[bytes]]:
        if not known_heads or any(len(value) not in (40, 64) or not COMMIT_ID.fullmatch(value)
                                  for value in known_heads):
            raise ApiPublicationError("request history requires full known commit IDs")
        owner, repository = self.repository.split("/", 1)
        variables = {"owner": owner, "repo": repository, "ref": "refs/heads/" + branch,
                     "path": GROUP_REQUEST_PREFIX + validate_identifier(group_id, "task ID") + ".json"}
        definitions = ["$owner:String!", "$repo:String!", "$ref:String!", "$path:String!"]
        comparisons = []
        for index, commit in enumerate(known_heads):
            key = "head" + str(index)
            variables[key] = commit
            definitions.append("$" + key + ":String!")
            comparisons.append("c%d:compare(headRef:$%s){aheadBy baseTarget{oid} headTarget{oid}}" % (index, key))
        query = ("query(" + ",".join(definitions) + "){repository(owner:$owner,name:$repo){"
                 "ref(qualifiedName:$ref){name target{oid ... on Commit{file(path:$path){"
                 "object{oid ... on Blob{byteSize isBinary text}}}}}" + "".join(comparisons) + "}}}")
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": self.watcher.authorization, "Accept": "application/vnd.github+json",
                   "Content-Type": "application/json", "User-Agent": "blue-green-pingpong"}
        try:
            self.connection.request("POST", "/graphql", body=body, headers=headers)
            response = self.connection.getresponse()
            raw = response.read(MAX_AGENT_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise ApiPublicationError("request API snapshot could not be read") from exc
        retry_seconds = api_rate_limit_seconds(response)
        if retry_seconds is not None:
            raise ApiRateLimit(retry_seconds)
        if response.status != 200 or len(raw) > MAX_AGENT_RESPONSE_BYTES:
            raise ApiPublicationError("request API snapshot is unavailable")
        try:
            payload = parse_json(raw, "request API snapshot")
        except BridgeError as exc:
            raise ApiPublicationError("request API snapshot is invalid") from exc
        data = payload.get("data")
        repository_data = data.get("repository") if isinstance(data, dict) else None
        ref = repository_data.get("ref") if isinstance(repository_data, dict) else None
        target = ref.get("target") if isinstance(ref, dict) else None
        head = target.get("oid") if isinstance(target, dict) else None
        if (payload.get("errors") or not isinstance(ref, dict) or ref.get("name") != branch or
                not isinstance(head, str) or len(head) not in (40, 64) or not COMMIT_ID.fullmatch(head)):
            raise ApiPublicationError("request API returned an invalid branch")
        for index, known in enumerate(known_heads):
            comparison = ref.get("c" + str(index))
            if not isinstance(comparison, dict):
                raise ApiPublicationError("request history could not be compared")
            base = comparison.get("baseTarget")
            previous = comparison.get("headTarget")
            if (not isinstance(base, dict) or base.get("oid") != head or
                    not isinstance(previous, dict) or previous.get("oid") != known or
                    type(comparison.get("aheadBy")) is not int or comparison["aheadBy"] != 0):
                raise ApiPublicationError("request history needs Git verification")
        entry = target.get("file")
        if entry is None:
            return head, None
        blob = entry.get("object") if isinstance(entry, dict) else None
        if not isinstance(blob, dict) or blob.get("isBinary") is not False or not isinstance(blob.get("text"), str):
            raise ApiPublicationError("request task file is unavailable through the API")
        try:
            content = blob["text"].encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ApiPublicationError("request API returned invalid text") from exc
        oid = blob.get("oid")
        if not isinstance(oid, str) or len(oid) not in (40, 64) or blob.get("byteSize") != len(content):
            raise ApiPublicationError("request API returned an invalid blob")
        hasher = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
        actual = hasher(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
        if actual != oid.lower():
            raise ApiPublicationError("request API blob does not match its object ID")
        return head, content


class ClientRequestHeads:
    """Persist observed branch heads without storing request contents or credentials."""

    def __init__(self, settings: Settings, git: GitTransport) -> None:
        self.path = settings.state_dir / "client_api.sqlite3"
        self.remote = git.remote_fingerprint()
        self.branch = settings.requests_branch
        settings.state_dir.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS request_heads (
                remote TEXT NOT NULL, branch TEXT NOT NULL, head TEXT NOT NULL,
                PRIMARY KEY (remote, branch, head)
            );
            CREATE TABLE IF NOT EXISTS oversized_groups (
                remote TEXT NOT NULL, branch TEXT NOT NULL, group_id TEXT NOT NULL,
                PRIMARY KEY (remote, branch, group_id)
            );
            CREATE TABLE IF NOT EXISTS request_api_cooldowns (
                remote TEXT NOT NULL, branch TEXT NOT NULL, retry_after REAL NOT NULL,
                PRIMARY KEY (remote, branch)
            );
        """)
        return connection

    def heads(self) -> List[str]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT head FROM request_heads WHERE remote = ? AND branch = ? ORDER BY head",
                                      (self.remote, self.branch)).fetchall()
        return [str(row[0]) for row in rows]

    def record(self, head: str, verified: Sequence[str] = ()) -> None:
        with closing(self.connect()) as connection, connection:
            connection.executemany("DELETE FROM request_heads WHERE remote = ? AND branch = ? AND head = ?",
                                   [(self.remote, self.branch, value) for value in verified])
            connection.execute("INSERT OR IGNORE INTO request_heads(remote, branch, head) VALUES (?, ?, ?)",
                               (self.remote, self.branch, head))

    def is_oversized(self, group_id: str) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT 1 FROM oversized_groups WHERE remote = ? AND branch = ? AND group_id = ?",
                                     (self.remote, self.branch, group_id)).fetchone()
        return row is not None

    def mark_oversized(self, group_id: str) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT OR IGNORE INTO oversized_groups(remote, branch, group_id) VALUES (?, ?, ?)",
                               (self.remote, self.branch, group_id))

    def api_ready(self) -> bool:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT retry_after FROM request_api_cooldowns WHERE remote = ? AND branch = ?",
                                     (self.remote, self.branch)).fetchone()
        return row is None or time.time() >= float(row[0])

    def defer_api(self, seconds: int) -> None:
        with closing(self.connect()) as connection, connection:
            connection.execute("INSERT INTO request_api_cooldowns(remote, branch, retry_after) VALUES (?, ?, ?) "
                               "ON CONFLICT(remote, branch) DO UPDATE SET retry_after = "
                               "MAX(request_api_cooldowns.retry_after, excluded.retry_after)",
                               (self.remote, self.branch, time.time() + seconds))


def request_cache_ref(git: GitTransport, settings: Settings) -> str:
    key = git.remote_fingerprint() + "\0" + settings.requests_branch
    return "refs/pingpong-client/" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def cached_request_base(git: GitTransport, settings: Settings, cache_ref: str) -> Optional[str]:
    """Use an API-confirmed local commit; fall back to Git if it is unavailable."""
    cached = git.run(["rev-parse", "--verify", "--quiet", cache_ref + "^{commit}"],
                     check=False).decode("ascii").strip()
    if not cached:
        return None
    try:
        watcher = GitHubRefWatcher(git, settings)
        try:
            remote_head = watcher.check()
        finally:
            watcher.close()
    except BridgeError:
        head = git.branch_tip(settings.requests_branch)
    else:
        try:
            head = git.commit(remote_head)
        except BridgeError:
            head = git.branch_tip(settings.requests_branch)
    if not git.is_ancestor(cached, head):
        # A GitHub API response can lag a successful push; confirm with Git before rejecting it.
        head = git.branch_tip(settings.requests_branch)
        if not git.is_ancestor(cached, head):
            raise BridgeError("request branch was rewritten since the previous submission")
    return head


def remember_request_commit(git: GitTransport, cache_ref: str, commit: str) -> None:
    """Keep a reachable cache ref without moving it backwards during local races."""
    for _ in range(4):
        old = git.run(["rev-parse", "--verify", "--quiet", cache_ref + "^{commit}"],
                      check=False).decode("ascii").strip()
        if old and not git.is_ancestor(old, commit):
            return
        try:
            git.run(["update-ref", cache_ref, commit, old or "0" * len(commit)])
            return
        except GitOperationError:
            continue
    LOGGER.warning("Could not update the local request cache")


def known_request_heads(git: GitTransport, cache_ref: str, state: ClientRequestHeads) -> List[str]:
    cached = git.run(["rev-parse", "--verify", "--quiet", cache_ref + "^{commit}"],
                     check=False).decode("ascii").strip()
    heads = state.heads()
    if cached:
        heads.append(cached)
    unique = sorted(set(heads))
    if any(len(head) not in (40, 64) or not COMMIT_ID.fullmatch(head) for head in unique):
        raise BridgeError("saved request history contains an invalid commit ID")
    return unique


def checked_request_base(git: GitTransport, settings: Settings, cache_ref: str,
                         state: ClientRequestHeads) -> Optional[str]:
    known = known_request_heads(git, cache_ref, state)
    if not known:
        return cached_request_base(git, settings, cache_ref)
    head = git.branch_tip(settings.requests_branch)
    for previous in known:
        if not git.is_ancestor(previous, head):
            raise BridgeError("request branch was rewritten since a previous submission")
    state.record(head, known)
    return head


def submit_group_fast(git: ConcurrentGitTransport, settings: Settings, cache_ref: str,
                      group_id: str, requests: Sequence[Dict[str, Any]]) -> Tuple[str, bool]:
    """Use exact-head API updates, with history-checked Git recovery on uncertainty."""
    group_id = validate_identifier(group_id, "task ID")
    state = ClientRequestHeads(settings, git)
    known = known_request_heads(git, cache_ref, state)
    api: Optional[GitHubRequestTransport] = None
    if known and len(known) <= 8 and state.api_ready() and not state.is_oversized(group_id):
        try:
            api = GitHubRequestTransport(git, settings)
        except BridgeError as exc:
            state.defer_api(60)
            LOGGER.info("Request API unavailable; using Git: %s", type(exc).__name__)
    if api is not None:
        try:
            for _ in range(4):
                known = known_request_heads(git, cache_ref, state)
                if len(known) > 8:
                    break
                try:
                    head, old = api.snapshot(settings.requests_branch, group_id, known)
                except ApiRateLimit as exc:
                    state.defer_api(exc.retry_seconds)
                    LOGGER.info("Request API is rate limited; using Git")
                    break
                except ApiPublicationError as exc:
                    state.defer_api(60)
                    LOGGER.info("Request API snapshot unavailable; using Git: %s", type(exc).__name__)
                    break
                state.record(head, known)
                content = append_group_bytes(old, group_id, requests)
                if content is None:
                    return head, True
                if 4 * ((len(content) + 2) // 3) + 2048 > MAX_API_COMMIT_BODY_BYTES:
                    state.mark_oversized(group_id)
                    break
                try:
                    commit = api.publish(settings.requests_branch, head,
                                         GROUP_REQUEST_PREFIX + group_id + ".json", content,
                                         "pingpong: append task commands")
                except ApiHeadConflict:
                    LOGGER.info("Request branch advanced; retrying atomic append")
                    continue
                except ApiPayloadTooLarge:
                    state.mark_oversized(group_id)
                    break
                except ApiRateLimit as exc:
                    state.defer_api(exc.retry_seconds)
                    LOGGER.info("Request API is rate limited; using Git")
                    break
                except ApiPublicationError as exc:
                    state.defer_api(60)
                    LOGGER.info("Request API outcome needs Git verification: %s", type(exc).__name__)
                    break
                state.record(commit)
                return commit, True
        finally:
            api.close()
    base = checked_request_base(git, settings, cache_ref, state)
    commit = git.append_group_requests(group_id, requests, base_hint=base, confirm_remote=False)
    state.record(commit)
    return commit, False


def connect_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, request_commit TEXT NOT NULL, request_hash TEXT NOT NULL,
            request_json TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT NOT NULL,
            published_state TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sessions (
            alias TEXT PRIMARY KEY, native_id TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS task_groups (
            job_id TEXT PRIMARY KEY, group_id TEXT NOT NULL, command_id TEXT NOT NULL,
            UNIQUE(group_id, command_id)
        );
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            job_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL, failures INTEGER NOT NULL,
            last_error TEXT NOT NULL, next_retry_at REAL NOT NULL, last_attempt TEXT NOT NULL
        );
    """)
    return connection


def acquire_service_lock(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=0)
    try:
        connection.execute("BEGIN EXCLUSIVE")
    except sqlite3.OperationalError as exc:
        connection.close()
        raise BridgeError("another service already holds the state-directory lock") from exc
    return connection


def metadata(connection: sqlite3.Connection, key: str) -> Optional[str]:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))


def result_generation(connection: sqlite3.Connection) -> str:
    return metadata(connection, "atomic_attempt_generation") or "0"


def result_generation_verified(connection: sqlite3.Connection) -> bool:
    return result_generation(connection) == (metadata(connection, "verified_atomic_generation") or "0")


def atomic_api_available(connection: sqlite3.Connection) -> bool:
    return time.time() >= float(metadata(connection, "atomic_api_retry_at") or "0")


def defer_atomic_api(connection: sqlite3.Connection, retry_seconds: int) -> None:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = float(metadata(connection, "atomic_api_retry_at") or "0")
        set_metadata(connection, "atomic_api_retry_at", str(max(current, time.time() + retry_seconds)))


def begin_atomic_attempt(connection: sqlite3.Connection) -> str:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        generation = str(int(result_generation(connection)) + 1)
        set_metadata(connection, "atomic_attempt_generation", generation)
    return generation


def verify_atomic_generation(connection: sqlite3.Connection, generation: str) -> bool:
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        if result_generation(connection) != generation:
            return False
        set_metadata(connection, "verified_atomic_generation", generation)
        return True


def record_results_head(connection: sqlite3.Connection, git: GitTransport, head: str) -> None:
    """Advance the recorded head without regressing when pushes finish out of order."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = metadata(connection, "results_head")
        if previous and git.is_ancestor(head, previous):
            return
        if previous and not git.is_ancestor(previous, head):
            raise BridgeError("result branch was rewritten; stop and inspect it")
        set_metadata(connection, "results_head", head)


def record_fetched_results_head(connection: sqlite3.Connection, git: GitTransport, head: str,
                                before_fetch: Optional[str]) -> bool:
    """Reject rewrites while allowing a fetch that raced with a verified push."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        current = metadata(connection, "results_head")
        if current and not git.is_ancestor(current, head):
            if current != before_fetch and git.is_ancestor(head, current):
                return False
            raise BridgeError("result branch was rewritten; stop and inspect it")
        set_metadata(connection, "results_head", head)
    return True


def make_result(job: sqlite3.Row, state: str, message: str,
                agent_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    request = json.loads(job["request_json"])
    previous = json.loads(job["result_json"])
    timing = dict(previous.get("timing", {}))
    now = timestamp()
    marker = {"accepted": "accepted_at", "running": "running_at"}.get(state)
    if marker is not None:
        timing.setdefault(marker, now)
    elif state in TERMINAL_STATES:
        timing.setdefault("finished_at", now)
    result = {
        "version": 1, "id": request.get("id", job["id"]), "request_commit": job["request_commit"],
        "request_sha256": job["request_hash"], "action": request.get("action"),
        "session": request.get("session"), "state": state, "updated_at": now,
        "message": message,
    }
    if timing:
        result["timing"] = timing
    if agent_report is not None:
        result["agent_report"] = agent_report
        result["local_adapter_stdout"] = "logs/" + str(job["id"]) + ".stdout"
        result["local_adapter_stderr"] = "logs/" + str(job["id"]) + ".stderr"
    return result


def update_job(connection: sqlite3.Connection, task_id: str, state: str,
               result: Dict[str, Any], published: str = "") -> None:
    if state in TERMINAL_STATES:
        connection.execute("UPDATE delivery_attempts SET failures = 0, next_retry_at = 0 "
                           "WHERE job_id = ? AND EXISTS (SELECT 1 FROM jobs WHERE id = ? "
                           "AND state NOT IN ('completed', 'failed', 'needs_input', 'unknown'))", (task_id, task_id))
    connection.execute("UPDATE jobs SET state = ?, result_json = ?, published_state = ? WHERE id = ?",
                       (state, json_bytes(result).decode("utf-8"), published, task_id))
    connection.commit()


def mark_started_unknown(connection: sqlite3.Connection, row: sqlite3.Row, message: str) -> None:
    result = make_result(row, "unknown", message)
    prior_result = json.loads(row["result_json"])
    if prior_result.get("native_session_id"):
        result["native_session_id"] = prior_result["native_session_id"]
    if result["action"] == "command.execute":
        result["local_command_output"] = "commands/" + str(row["id"])
    else:
        result["local_adapter_stdout"] = "logs/" + str(row["id"]) + ".stdout"
        result["local_adapter_stderr"] = "logs/" + str(row["id"]) + ".stderr"
    update_job(connection, row["id"], "unknown", result)


def recover_interrupted(connection: sqlite3.Connection) -> None:
    rows = connection.execute("SELECT * FROM jobs WHERE state = 'running'").fetchall()
    for row in rows:
        mark_started_unknown(connection, row, "Service stopped during execution; no automatic retry.")


def ingest_requests(connection: sqlite3.Connection, git: GitTransport, settings: Settings,
                    head: Optional[str] = None) -> None:
    if head is None:
        head = git.branch_tip(settings.requests_branch)
    binding = json.dumps([str(settings.git_dir), settings.remote, git.remote_fingerprint(),
                          settings.requests_branch, settings.results_branch], ensure_ascii=False)
    old_binding = metadata(connection, "binding")
    if old_binding is not None and old_binding != binding:
        raise BridgeError("state directory is bound to a different repository or branch")
    previous = metadata(connection, "request_head")
    if previous is None:
        previous = head if settings.start_after == "HEAD" else git.commit(settings.start_after)
        if not git.is_ancestor(previous, head):
            raise BridgeError("start_after is not an ancestor of the request branch")
        with connection:
            set_metadata(connection, "binding", binding)
            set_metadata(connection, "request_head", previous)
            set_metadata(connection, "group_request_head", previous)
        if previous == head:
            LOGGER.info("Baseline recorded; only future requests will run")
            return
    group_previous = metadata(connection, "group_request_head") or previous
    changes = [(commit, task_id, data, None) for commit, task_id, data in git.added_requests(previous, head)]
    changes.extend(git.added_group_requests(group_previous, head))
    if changes:
        oldest = group_previous if git.is_ancestor(group_previous, previous) else previous
        commits = git.run(["rev-list", "--reverse", "--first-parent", oldest + ".." + head]).splitlines()
        order = {commit.decode("ascii"): index for index, commit in enumerate(commits)}
        changes.sort(key=lambda change: order[change[0]])
    with connection:
        for commit, command_id, data, group_id in changes:
            task_id = group_job_id(group_id, command_id) if group_id is not None else command_id
            digest = hashlib.sha256(data).hexdigest()
            old = connection.execute("SELECT request_hash FROM jobs WHERE id = ?", (task_id,)).fetchone()
            if old is not None:
                if old["request_hash"] != digest:
                    raise BridgeError("duplicate task ID has different content")
                old_group = connection.execute("SELECT group_id, command_id FROM task_groups WHERE job_id = ?",
                                               (task_id,)).fetchone()
                if ((old_group["group_id"] if old_group is not None else None) != group_id or
                        (old_group is not None and old_group["command_id"] != command_id)):
                    raise BridgeError("command ID already belongs to another task")
                continue
            try:
                request = validate_request(data, command_id)
                error = ""
            except BridgeError as exc:
                request = {"version": 1, "id": command_id, "action": "invalid"}
                error = str(exc)
            state = "failed" if error else "queued"
            received_at = timestamp()
            initial = {"version": 1, "id": command_id, "request_commit": commit,
                       "request_sha256": digest, "action": request.get("action"),
                       "session": request.get("session"), "state": state,
                       "updated_at": received_at, "timing": {"queued_at": received_at},
                       "message": error or "Queued."}
            connection.execute("INSERT INTO jobs(id, request_commit, request_hash, request_json, state, result_json) "
                               "VALUES (?, ?, ?, ?, ?, ?)",
                               (task_id, commit, digest, json.dumps(request, ensure_ascii=False),
                                state, json_bytes(initial).decode("utf-8")))
            if group_id is not None:
                connection.execute("INSERT INTO task_groups(job_id, group_id, command_id) VALUES (?, ?, ?)",
                                   (task_id, group_id, command_id))
        set_metadata(connection, "request_head", head)
        set_metadata(connection, "group_request_head", head)


def publish_job(connection: sqlite3.Connection, git: GitTransport, settings: Settings,
                job: sqlite3.Row) -> None:
    if job["published_state"] == job["state"]:
        return
    result = json.loads(job["result_json"])
    group_row = connection.execute("SELECT group_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
    group_id = group_row["group_id"] if group_row is not None else None
    if result.get("command_result") is not None:
        commit = git.publish_command_result(job, result, metadata(connection, "results_head"),
                                            confirm_remote=settings.result_confirmation == "fetch", group_id=group_id)
    elif group_id is not None:
        commit = git.publish_group_state(group_id, job, result, metadata(connection, "results_head"),
                                         confirm_remote=settings.result_confirmation == "fetch")
    else:
        data = job["result_json"].encode("utf-8")
        commit = git.publish(settings.results_branch, RESULT_PREFIX + job["id"] + ".json", data,
                             request_hash=job["request_hash"], base_hint=metadata(connection, "results_head"),
                             confirm_remote=settings.result_confirmation == "fetch")
    record_results_head(connection, git, commit)
    with connection:
        connection.execute("UPDATE jobs SET published_state = ? WHERE id = ? AND state = ?",
                           (job["state"], job["id"], job["state"]))


def prepare_adapter_input(connection: sqlite3.Connection, job: sqlite3.Row) -> Dict[str, Any]:
    request = json.loads(job["request_json"])
    action = request["action"]
    alias = request.get("session") or metadata(connection, "active_session")
    if not alias:
        raise BridgeError("no session selected")
    row = connection.execute("SELECT native_id, checkpoint FROM sessions WHERE alias = ?", (alias,)).fetchone()
    if action in {"session.new", "session.attach"}:
        if row is not None:
            raise BridgeError("session alias already exists; use a new alias")
        native_id = request.get("native_session_id") if action == "session.attach" else str(uuid.uuid4())
    else:
        if row is None:
            raise BridgeError("session alias is not registered")
        native_id = row["native_id"]
    context = dict(request.get("context", {}))
    group = connection.execute("SELECT group_id, command_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
    if group is not None:
        relay_context = {"task_id": group["group_id"], "command_id": group["command_id"]}
        if "pingpong_relay" in context and context["pingpong_relay"] != relay_context:
            raise BridgeError("context reserves pingpong_relay for task identity")
        context["pingpong_relay"] = relay_context
    return {
        "version": 1, "id": job["id"], "request_commit": job["request_commit"],
        "request_sha256": job["request_hash"], "action": action, "session": alias,
        "native_session_id": native_id, "message": request.get("message", ""),
        "context": context,
        "previous_checkpoint": "" if row is None else row["checkpoint"],
    }


def adapter_response(settings: Settings, adapter_input: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    logs = settings.state_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    task_id = adapter_input["id"]
    stdout_path = logs / (task_id + ".stdout")
    stderr_path = logs / (task_id + ".stderr")
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    if settings.git_config_global is not None:
        environment["GIT_CONFIG_GLOBAL"] = str(settings.git_config_global)
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.run(settings.adapter_argv, input=json_bytes(adapter_input),
                                     stdout=stdout, stderr=stderr, env=environment,
                                     timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BridgeError("adapter timed out; child processes may still be running") from exc
    except OSError as exc:
        raise BridgeError("adapter could not be started") from exc
    if process.returncode != 0:
        raise BridgeError("adapter exited with code %d" % process.returncode)
    try:
        with stdout_path.open("rb") as stream:
            data = stream.read(MAX_AGENT_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise BridgeError("cannot read adapter output") from exc
    if len(data) > MAX_AGENT_RESPONSE_BYTES:
        raise BridgeError("adapter response exceeds size limit")
    response = parse_json(data, "adapter response")
    if response.get("version") != 1 or response.get("status") not in {"completed", "failed", "needs_input"}:
        raise BridgeError("adapter returned an invalid status or version")
    allowed = {"version", "status", "native_session_id", "summary", "command_evidence", "artifacts", "context", "checkpoint"}
    return {key: value for key, value in response.items() if key in allowed}


def complete_job(connection: sqlite3.Connection, job: sqlite3.Row,
                 adapter_input: Dict[str, Any], response: Dict[str, Any]) -> None:
    status = response["status"]
    state = {"completed": "completed", "failed": "failed", "needs_input": "needs_input"}[status]
    action = adapter_input["action"]
    alias = adapter_input["session"]
    native = response.get("native_session_id")
    expected = adapter_input["native_session_id"]
    if status == "completed":
        if not isinstance(native, str) or not native or len(native) > 512:
            raise BridgeError("completed adapter response has no native session ID")
        if native != expected:
            raise BridgeError("adapter returned a different session ID")
    result = make_result(job, state, "Agent invocation completed; its command outcomes are agent-reported.", response)
    result["session"] = alias
    with connection:
        if status == "completed" and action in {"session.new", "session.attach"}:
            connection.execute("INSERT INTO sessions(alias, native_id) VALUES (?, ?)", (alias, native))
        if status == "completed" and action in {"session.new", "session.attach", "session.switch"}:
            set_metadata(connection, "active_session", alias)
        checkpoint = response.get("checkpoint")
        if status == "completed" and isinstance(checkpoint, str):
            connection.execute("UPDATE sessions SET checkpoint = ? WHERE alias = ?", (checkpoint, alias))
        connection.execute("UPDATE jobs SET state = ?, result_json = ?, published_state = '' WHERE id = ?",
                           (state, json_bytes(result).decode("utf-8"), job["id"]))


def decode_inline_output(info: Dict[str, Any]) -> bytes:
    inline = info.get("inline_base64")
    if not isinstance(inline, str) or len(inline) > 4 * ((INLINE_OUTPUT_BYTES + 2) // 3) or info.get("parts") != []:
        raise BridgeError("inline command output manifest is invalid")
    try:
        data = base64.b64decode(inline, validate=True)
    except ValueError as exc:
        raise BridgeError("inline command output encoding is invalid") from exc
    if (len(data) > INLINE_OUTPUT_BYTES or len(data) != info.get("size") or
            hashlib.sha256(data).hexdigest() != info.get("sha256") or
            base64.b64encode(data).decode("ascii") != inline):
        raise BridgeError("inline command output failed verification")
    return data


def verify_inline_group_entries(settings: Settings,
                                entries: Sequence[Tuple[sqlite3.Row, Dict[str, Any]]]) -> None:
    for job, result in entries:
        command = result.get("command_result")
        if not isinstance(command, dict):
            raise BridgeError("result batch contains a non-command result")
        for stream in ("stdout", "stderr"):
            info = command.get(stream)
            if not isinstance(info, dict) or info.get("parts") != []:
                raise BridgeError("result batch requires inline or empty output")
            if "inline_base64" in info:
                data = decode_inline_output(info)
            elif info.get("size") == 0 and info.get("sha256") == hashlib.sha256(b"").hexdigest():
                data = b""
            else:
                raise BridgeError("empty command output manifest is invalid")
            snapshot = settings.state_dir / "commands" / job["id"] / (stream + ".snapshot")
            with snapshot.open("rb") as source:
                if source.read(INLINE_OUTPUT_BYTES + 1) != data:
                    raise BridgeError("local command output changed before publication")


def command_output_info(snapshot: Path, task_id: str, stream: str,
                        chunk_bytes: int = COMMAND_CHUNK_BYTES, omit_empty: bool = False,
                        inline_small: bool = False) -> Dict[str, Any]:
    """Describe a snapshot as ordered, independently verifiable raw Git blobs."""
    if inline_small and 0 < snapshot.stat().st_size <= INLINE_OUTPUT_BYTES:
        with snapshot.open("rb") as source:
            data = source.read(INLINE_OUTPUT_BYTES + 1)
        if 0 < len(data) <= INLINE_OUTPUT_BYTES:
            return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "parts": [],
                    "inline_base64": base64.b64encode(data).decode("ascii")}
    digest = hashlib.sha256()
    size = 0
    parts = []
    with snapshot.open("rb") as source:
        while True:
            chunk = source.read(chunk_bytes)
            if not chunk and not parts and omit_empty:
                break
            if not chunk and parts:
                break
            path = OUTPUT_PREFIX + task_id + "/" + stream + ".%06d" % len(parts)
            parts.append({"path": path, "size": len(chunk), "sha256": hashlib.sha256(chunk).hexdigest()})
            digest.update(chunk)
            size += len(chunk)
            if not chunk:
                break
    return {"size": size, "sha256": digest.hexdigest(), "parts": parts}


def log_execution(connection: sqlite3.Connection, job: sqlite3.Row, request: Dict[str, Any]) -> None:
    description = request.get("description")
    if isinstance(description, str):
        description = " ".join("".join(char if char.isprintable() else " " for char in description).split())[:200]
    if not isinstance(description, str) or not description:
        description = "未填写中文说明"
    labels = {"command.execute": "命令", "task.execute": "代理任务", "session.new": "新建会话",
              "session.attach": "关联会话", "session.switch": "切换会话", "session.checkpoint": "记录会话"}
    group = connection.execute("SELECT group_id, command_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
    identity = ("任务 %s / 指令 %s" % (group["group_id"], group["command_id"]) if group is not None
                else "任务 %s" % job["id"])
    LOGGER.log(TASK_LOG_LEVEL, "执行%s：%s [%s]", labels.get(request["action"], "操作"), description, identity)


def execute_command_job(connection: sqlite3.Connection, git: GitTransport, settings: Settings,
                        job: sqlite3.Row, asynchronous_progress: bool = False) -> None:
    request = json.loads(job["request_json"])
    command = request["command"]
    cwd = Path(command["cwd"]).expanduser()
    timeout = request.get("timeout_seconds", settings.default_timeout_seconds)
    if not cwd.is_absolute() or not cwd.is_dir():
        update_job(connection, job["id"], "failed", make_result(job, "failed", "Command working directory is unavailable."))
        return
    if timeout > settings.max_timeout_seconds:
        update_job(connection, job["id"], "failed", make_result(job, "failed", "Command timeout exceeds service maximum."))
        return
    directory = settings.state_dir / "commands" / job["id"]
    long_script = None
    if command["mode"] == "powershell":
        encoded = base64.b64encode(command["script"].encode("utf-16le")).decode("ascii")
        if len(encoded) > 30000:
            long_script = directory / "command.ps1"
            argv = [settings.powershell_executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(long_script)]
        else:
            argv = [settings.powershell_executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    else:
        argv = command["argv"]
    if directory.exists():
        update_job(connection, job["id"], "failed", make_result(job, "failed", "Command output directory already exists."))
        return
    running = make_result(job, "running", "Starting local command; interruption may leave an unknown outcome.")
    if command.get("report_running", True) and not asynchronous_progress:
        group_row = connection.execute("SELECT group_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
        if group_row is not None:
            commit = git.publish_group_state(group_row["group_id"], job, running,
                                             base_hint=metadata(connection, "results_head"))
        else:
            commit = git.publish(settings.results_branch, RESULT_PREFIX + job["id"] + ".json", json_bytes(running),
                                 request_hash=job["request_hash"], base_hint=metadata(connection, "results_head"))
        record_results_head(connection, git, commit)
        update_job(connection, job["id"], "running", running, "running")
    else:
        update_job(connection, job["id"], "running", running)
    job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    try:
        directory.mkdir(parents=True, exist_ok=False)
        if long_script is not None:
            long_script.write_text(command["script"], encoding="utf-8-sig")
    except OSError:
        update_job(connection, job["id"], "failed", make_result(job, "failed", "Cannot prepare command files."))
        return
    environment = git.environment()
    command_started_at = timestamp()
    started = time.monotonic()
    timed_out = False
    exit_code = None
    launch_error = None
    try:
        with (directory / "stdout.live").open("xb") as stdout, (directory / "stderr.live").open("xb") as stderr:
            try:
                log_execution(connection, job, request)
                process = subprocess.run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                         env=environment, timeout=timeout, check=False)
                exit_code = process.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
            except OSError as exc:
                launch_error = type(exc).__name__
        command_finished_at = timestamp()
        process_duration = time.monotonic() - started
        for stream in ("stdout", "stderr"):
            shutil.copyfile(directory / (stream + ".live"), directory / (stream + ".snapshot"))
        grouped = connection.execute("SELECT 1 FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone() is not None
        outputs = {stream: command_output_info(directory / (stream + ".snapshot"), job["id"], stream,
                                              omit_empty=grouped, inline_small=grouped)
                   for stream in ("stdout", "stderr")}
    except OSError as exc:
        result = make_result(job, "unknown", "Could not preserve command output; inspect local files.")
        result["local_command_output"] = "commands/" + str(job["id"])
        update_job(connection, job["id"], "unknown", result)
        LOGGER.error("Could not preserve command output: %s", type(exc).__name__)
        return
    state = "unknown" if timed_out else "failed" if launch_error or exit_code != 0 else "completed"
    message = "Command timed out; captured output is partial and child processes may still be running." if timed_out else (
        "Command could not be started." if launch_error else "Local command exited; output is captured without decoding.")
    result = make_result(job, state, message)
    result["local_command_output"] = "commands/" + str(job["id"])
    result["command_result"] = {"exit_code": exit_code, "timed_out": timed_out, "launch_error": launch_error,
                                "duration_seconds": round(time.monotonic() - started, 3), "stdout": outputs["stdout"],
                                "stderr": outputs["stderr"], "output_complete": not timed_out,
                                "started_at": command_started_at, "finished_at": command_finished_at,
                                "process_duration_seconds": round(process_duration, 3)}
    update_job(connection, job["id"], state, result)


def execute_job(connection: sqlite3.Connection, git: GitTransport, settings: Settings,
                job: sqlite3.Row, asynchronous_progress: bool = False) -> None:
    if json.loads(job["request_json"]).get("action") == "command.execute":
        execute_command_job(connection, git, settings, job, asynchronous_progress)
        return
    if not settings.adapter_argv:
        update_job(connection, job["id"], "failed", make_result(
            job, "failed", "Agent adapter is not configured; set adapter_argv before agent actions."))
        return
    try:
        adapter_input = prepare_adapter_input(connection, job)
        request = json.loads(job["request_json"])
        timeout = setting_int(request.get("timeout_seconds", settings.default_timeout_seconds),
                              "timeout_seconds", 1, settings.max_timeout_seconds)
    except BridgeError as exc:
        update_job(connection, job["id"], "failed", make_result(job, "failed", str(exc)))
        return
    running = make_result(job, "running", "Starting adapter; interruption may leave an unknown outcome.")
    running["native_session_id"] = adapter_input["native_session_id"]
    group_row = connection.execute("SELECT group_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
    if group_row is not None:
        commit = git.publish_group_state(group_row["group_id"], job, running)
    else:
        commit = git.publish(settings.results_branch, RESULT_PREFIX + job["id"] + ".json", json_bytes(running),
                             request_hash=job["request_hash"])
    record_results_head(connection, git, commit)
    update_job(connection, job["id"], "running", running, "running")
    job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    try:
        log_execution(connection, job, request)
        response = adapter_response(settings, adapter_input, timeout)
        complete_job(connection, job, adapter_input, response)
    except BridgeError as exc:
        result = make_result(job, "unknown", str(exc))
        result["native_session_id"] = adapter_input["native_session_id"]
        result["local_adapter_stdout"] = "logs/" + str(job["id"]) + ".stdout"
        result["local_adapter_stderr"] = "logs/" + str(job["id"]) + ".stderr"
        update_job(connection, job["id"], "unknown", result)


def cycle(connection: sqlite3.Connection, git: GitTransport, settings: Settings) -> None:
    started = time.monotonic()
    request_head, result_head = git.branch_tips((settings.requests_branch, settings.results_branch))
    LOGGER.info("Git branch synchronization took %.3f seconds", time.monotonic() - started)
    ingest_requests(connection, git, settings, request_head)
    previous_result_head = metadata(connection, "results_head")
    if previous_result_head is not None and not git.is_ancestor(previous_result_head, result_head):
        raise BridgeError("result branch was rewritten; stop and inspect it")
    with connection:
        set_metadata(connection, "results_head", result_head)
    rows = connection.execute("SELECT * FROM jobs ORDER BY rowid").fetchall()
    for row in rows:
        if row["state"] in TERMINAL_STATES:
            publish_job(connection, git, settings, row)
            continue
        if row["state"] == "queued":
            result = make_result(row, "accepted", "Request accepted; execution has not started.")
            update_job(connection, row["id"], "accepted", result)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        if row["state"] == "accepted":
            if json.loads(row["request_json"]).get("action") != "command.execute":
                publish_job(connection, git, settings, row)
            execute_job(connection, git, settings, row)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            if row["state"] in TERMINAL_STATES:
                publish_job(connection, git, settings, row)


def pending_jobs(connection: sqlite3.Connection) -> bool:
    row = connection.execute("SELECT 1 FROM jobs WHERE state NOT IN ('completed', 'failed', 'needs_input', 'unknown') "
                             "OR published_state != state LIMIT 1").fetchone()
    return row is not None


def task_worker(settings: Settings, task_id: str) -> bool:
    """Execute one claimed task with its own database connection and Git transport."""
    connection = connect_database(settings.state_dir / "state.sqlite3")
    try:
        try:
            git = ConcurrentGitTransport(settings)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (task_id,)).fetchone()
            if row is None or row["state"] != "accepted":
                return True
            if json.loads(row["request_json"]).get("action") != "command.execute":
                publish_job(connection, git, settings, row)
            execute_job(connection, git, settings, row, asynchronous_progress=True)
            return True
        except (BridgeError, sqlite3.Error, OSError) as exc:
            LOGGER.warning("Task worker %s failed: %s", task_id, type(exc).__name__)
        except Exception as exc:  # Isolate unexpected failures to this task.
            LOGGER.error("Task worker %s failed unexpectedly: %s", task_id, type(exc).__name__)
        connection.rollback()
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (task_id,)).fetchone()
        if row is not None and row["state"] == "running":
            mark_started_unknown(connection, row, "Task worker stopped; execution will not be retried.")
        return False
    finally:
        connection.close()


def record_delivery_attempt(connection: sqlite3.Connection, task_id: str, error: Optional[Exception],
                            attempted_state: Optional[str]) -> None:
    row = connection.execute("SELECT attempts, failures FROM delivery_attempts WHERE job_id = ?", (task_id,)).fetchone()
    attempts = (row["attempts"] if row is not None else 0) + 1
    failures = (row["failures"] if row is not None else 0) + 1 if error is not None else 0
    current = connection.execute("SELECT state FROM jobs WHERE id = ?", (task_id,)).fetchone()
    if error is not None and attempted_state is not None and current is not None and current["state"] != attempted_state:
        failures = 0  # A failed progress update must not postpone the finished result.
    reason = (error.kind if isinstance(error, (PushError, GitOperationError)) else
              type(error).__name__) if error is not None else ""
    next_retry = (time.time() + min(300, 2 ** min(failures, 9))
                  if failures and not isinstance(error, ApiPayloadTooLarge) else 0.0)
    with connection:
        connection.execute("INSERT OR REPLACE INTO delivery_attempts "
                           "(job_id, attempts, failures, last_error, next_retry_at, last_attempt) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (task_id, attempts, failures, reason, next_retry, timestamp()))


def recover_command_publication(connection: sqlite3.Connection, git: ConcurrentGitTransport,
                                settings: Settings, job: sqlite3.Row, failure_kind: str) -> bool:
    """Retry a large saved output in bounded commits without running the command again."""
    result = json.loads(job["result_json"])
    command = result.get("command_result")
    if not isinstance(command, dict):
        return False
    total = sum(command[stream]["size"] for stream in ("stdout", "stderr"))
    if total < RECOVERY_CHUNK_BYTES or total > MAX_RECOVERY_OUTPUT_BYTES:
        return False
    group_row = connection.execute("SELECT group_id FROM task_groups WHERE job_id = ?", (job["id"],)).fetchone()
    group_id = group_row["group_id"] if group_row is not None else None
    head = git.branch_tip(settings.results_branch)
    existing = read_result(git, head, result["id"], group_id)
    if existing is not None and existing.get("request_sha256") != job["request_hash"]:
        raise BridgeError("remote result belongs to a different request")
    if existing is not None and existing.get("state") in TERMINAL_STATES:
        if existing != result:
            raise BridgeError("remote terminal result differs from the saved result")
        commit = git.publish_command_result(job, result, head, False, group_id)
        record_results_head(connection, git, commit)
        with connection:
            connection.execute("UPDATE jobs SET published_state = ? WHERE id = ? AND state = ?",
                               (job["state"], job["id"], job["state"]))
        return True
    if existing is None or existing.get("state") != "delivery_pending":
        pending = make_result(job, "delivery_pending", "Command finished; output delivery is being retried.")
        pending["execution_state"] = job["state"]
        pending["delivery_error"] = failure_kind
        if group_id is not None:
            head = git.publish_group_state(group_id, job, pending, head, False)
        else:
            head = git.publish(settings.results_branch, RESULT_PREFIX + job["id"] + ".json", json_bytes(pending),
                               request_hash=job["request_hash"], base_hint=head, confirm_remote=False)
        record_results_head(connection, git, head)
    for stream in ("stdout", "stderr"):
        snapshot = settings.state_dir / "commands" / job["id"] / (stream + ".snapshot")
        revised = command_output_info(snapshot, job["id"], stream, RECOVERY_CHUNK_BYTES,
                                      group_id is not None, group_id is not None)
        if revised["size"] != command[stream]["size"] or revised["sha256"] != command[stream]["sha256"]:
            raise BridgeError("saved command output changed before recovery")
        command[stream] = revised
    update_job(connection, job["id"], job["state"], result, job["published_state"])
    segments = []
    for stream in ("stdout", "stderr"):
        snapshot = settings.state_dir / "commands" / job["id"] / (stream + ".snapshot")
        with snapshot.open("rb") as source:
            for part in command[stream]["parts"]:
                data = source.read(part["size"])
                if len(data) != part["size"] or hashlib.sha256(data).hexdigest() != part["sha256"]:
                    raise BridgeError("saved command output changed during recovery")
                if data:
                    segments.append((part["path"], data))
    # The last chunk travels with the final manifest, so consumers never see a completed partial result.
    for path, data in segments[:-1]:
        head = git.publish(settings.results_branch, path, data, immutable=True, base_hint=head, confirm_remote=False)
        record_results_head(connection, git, head)
    current = connection.execute("SELECT * FROM jobs WHERE id = ?", (job["id"],)).fetchone()
    publish_job(connection, git, settings, current)
    LOGGER.info("Recovered command result %s in %d bounded output chunks", job["id"], len(segments))
    return True


def publication_worker(settings: Settings, task_id: str) -> bool:
    """Publish one durable result without holding an execution slot."""
    connection = connect_database(settings.state_dir / "state.sqlite3")
    row = None
    try:
        try:
            git = ConcurrentGitTransport(settings)
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (task_id,)).fetchone()
            if row is not None and row["state"] in TERMINAL_STATES | {"running"}:
                try:
                    publish_job(connection, git, settings, row)
                except PushError as exc:
                    if (row["state"] not in TERMINAL_STATES or
                            exc.kind in {"authentication", "http_401", "http_429", "start_failed"} or
                            not recover_command_publication(connection, git, settings, row, exc.kind)):
                        raise
                record_delivery_attempt(connection, task_id, None, row["state"])
            return True
        except (BridgeError, sqlite3.Error, OSError) as exc:
            connection.rollback()
            record_delivery_attempt(connection, task_id, exc, row["state"] if row is not None else None)
            reason = exc.kind if isinstance(exc, (PushError, GitOperationError)) else type(exc).__name__
            LOGGER.warning("Result publication %s failed: %s", task_id, reason)
            return False
        except Exception as exc:  # Isolate unexpected failures to this publication.
            connection.rollback()
            LOGGER.error("Result publication %s failed unexpectedly: %s", task_id, type(exc).__name__)
            return False
    finally:
        connection.close()


def inline_batch_candidate(row: sqlite3.Row) -> bool:
    if row["state"] not in TERMINAL_STATES or row["delivery_failures"]:
        return False
    result = json.loads(row["result_json"])
    command = result.get("command_result")
    if result.get("action") != "command.execute" or not isinstance(command, dict):
        return False
    return all(isinstance(command.get(stream), dict) and command[stream].get("parts") == [] and
               isinstance(command[stream].get("size"), int) and 0 <= command[stream]["size"] <= INLINE_OUTPUT_BYTES
               for stream in ("stdout", "stderr"))


def batch_publication_worker(settings: Settings, task_ids: Sequence[str], group_id: str) -> bool:
    """Publish finished small results together; on failure let individual retries handle them."""
    connection = connect_database(settings.state_dir / "state.sqlite3")
    entries: List[Tuple[sqlite3.Row, Dict[str, Any]]] = []
    try:
        try:
            git = ConcurrentGitTransport(settings)
            for task_id in task_ids:
                row = connection.execute("SELECT jobs.*, task_groups.group_id AS relay_group_id "
                                         "FROM jobs JOIN task_groups ON task_groups.job_id = jobs.id "
                                         "WHERE jobs.id = ?", (task_id,)).fetchone()
                if row is not None and row["relay_group_id"] == group_id and row["state"] in TERMINAL_STATES:
                    if row["published_state"] != row["state"]:
                        entries.append((row, json.loads(row["result_json"])))
            if len(entries) == 1:
                publish_job(connection, git, settings, entries[0][0])
            elif entries:
                commit = git.publish_inline_group_results(
                    group_id, entries, metadata(connection, "results_head"),
                    confirm_remote=settings.result_confirmation == "fetch")
                record_results_head(connection, git, commit)
                with connection:
                    for row, _ in entries:
                        connection.execute("UPDATE jobs SET published_state = ? WHERE id = ? AND state = ?",
                                           (row["state"], row["id"], row["state"]))
            for row, _ in entries:
                record_delivery_attempt(connection, row["id"], None, row["state"])
            return True
        except Exception as exc:  # Failures must leave each saved result available for individual retry.
            connection.rollback()
            for task_id in task_ids:
                current = connection.execute("SELECT state FROM jobs WHERE id = ?", (task_id,)).fetchone()
                record_delivery_attempt(connection, task_id, exc, current["state"] if current is not None else None)
            reason = exc.kind if isinstance(exc, (PushError, GitOperationError)) else type(exc).__name__
            LOGGER.warning("Batch result publication failed: %s", reason)
            return False
    finally:
        connection.close()


def api_batch_publication_worker(settings: Settings, task_ids: Sequence[str], group_id: str) -> bool:
    """Publish only bounded inline results using an exact expected branch head."""
    connection = connect_database(settings.state_dir / "state.sqlite3")
    entries: List[Tuple[sqlite3.Row, Dict[str, Any]]] = []
    try:
        try:
            git = ConcurrentGitTransport(settings)
            for task_id in task_ids:
                row = connection.execute("SELECT jobs.*, task_groups.group_id AS relay_group_id "
                                         "FROM jobs JOIN task_groups ON task_groups.job_id = jobs.id "
                                         "WHERE jobs.id = ?", (task_id,)).fetchone()
                if row is not None and row["relay_group_id"] == group_id and row["state"] in TERMINAL_STATES:
                    if row["published_state"] != row["state"]:
                        entries.append((row, json.loads(row["result_json"])))
            if not entries:
                return True
            base = metadata(connection, "results_head")
            if base is None:
                raise ApiPublicationError("no verified result branch head is available")
            base = git.commit(base)
            verify_inline_group_entries(settings, entries)
            path = GROUP_RESULT_PREFIX + validate_identifier(group_id, "task ID") + ".json"
            content = git.group_results_bytes(base, group_id, entries)
            current = git.blob(base, path, MAX_GROUP_BYTES)
            generation = None
            if current == content:
                confirmed = base
            else:
                if 4 * ((len(content) + 2) // 3) > MAX_API_COMMIT_BODY_BYTES:
                    raise ApiPayloadTooLarge("API result exceeds the bounded request size")
                generation = begin_atomic_attempt(connection)
                publisher = GitHubAtomicTransport(git, settings)
                try:
                    commit = publisher.publish(settings.results_branch, base, path, content)
                finally:
                    publisher.close()
                confirmed = git.branch_tip(settings.results_branch)
                if not git.is_ancestor(base, confirmed) or not git.is_ancestor(commit, confirmed):
                    raise ApiPublicationError("atomic result commit is not on the fetched result branch")
                if any(read_result(git, confirmed, result["id"], group_id) != result for _, result in entries):
                    raise ApiPublicationError("atomic result contents could not be confirmed")
            record_results_head(connection, git, confirmed)
            if generation is not None:
                verify_atomic_generation(connection, generation)
            with connection:
                for row, _ in entries:
                    connection.execute("UPDATE jobs SET published_state = ? WHERE id = ? AND state = ?",
                                       (row["state"], row["id"], row["state"]))
            for row, _ in entries:
                record_delivery_attempt(connection, row["id"], None, row["state"])
            return True
        except (BridgeError, sqlite3.Error, OSError) as exc:
            connection.rollback()
            if isinstance(exc, ApiRateLimit):
                defer_atomic_api(connection, exc.retry_seconds)
            for task_id in task_ids:
                current = connection.execute("SELECT state FROM jobs WHERE id = ?", (task_id,)).fetchone()
                record_delivery_attempt(connection, task_id, exc, current["state"] if current is not None else None)
            LOGGER.warning("Atomic result publication deferred: %s", type(exc).__name__)
            return False
        except Exception as exc:  # A worker failure must not cause command reexecution.
            connection.rollback()
            for task_id in task_ids:
                current = connection.execute("SELECT state FROM jobs WHERE id = ?", (task_id,)).fetchone()
                record_delivery_attempt(connection, task_id, exc, current["state"] if current is not None else None)
            LOGGER.error("Atomic result publication failed unexpectedly: %s", type(exc).__name__)
            return False
    finally:
        connection.close()


@dataclass
class ActiveWorker:
    future: concurrent.futures.Future
    kind: str
    uses_api: bool = False


class ConcurrentDispatcher:
    """Bound command execution and publication independently of agent execution."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=settings.command_workers + 1 + settings.publish_workers)
        self.active: Dict[Tuple[str, str], ActiveWorker] = {}
        self.retry_after: Dict[Tuple[str, str], float] = {}
        self.failures: Dict[Tuple[str, str], int] = {}
        self.wakeup = threading.Event()
        self.batch_deadlines: Dict[str, float] = {}
        self.next_batch_at: Optional[float] = None

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def reap(self, connection: sqlite3.Connection) -> None:
        for key, worker in list(self.active.items()):
            if not worker.future.done():
                continue
            task_id, kind = key
            try:
                success = worker.future.result()
            except Exception as exc:  # An executor failure must not trigger a running task again.
                LOGGER.error("Worker %s failed outside its handler: %s", task_id, type(exc).__name__)
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (task_id,)).fetchone()
                if kind != "publish" and row is not None and row["state"] == "running":
                    mark_started_unknown(connection, row, "Task worker stopped; execution will not be retried.")
                success = False
            del self.active[key]
            if success:
                self.failures.pop(key, None)
                self.retry_after.pop(key, None)
            else:
                failures = self.failures.get(key, 0) + 1
                self.failures[key] = failures
                delay = 0 if kind == "publish" else min(300, 2 ** min(failures, 9))
                self.retry_after[key] = time.monotonic() + delay

    def schedule(self, connection: sqlite3.Connection, allow_execution: bool,
                 allow_git_publication: bool = True, allow_api_publication: bool = True) -> None:
        rows = connection.execute(
            "SELECT jobs.*, task_groups.group_id AS relay_group_id, "
            "COALESCE(delivery_attempts.failures, 0) AS delivery_failures, "
            "COALESCE(delivery_attempts.next_retry_at, 0) AS delivery_retry_at "
            "FROM jobs LEFT JOIN delivery_attempts ON delivery_attempts.job_id = jobs.id "
            "LEFT JOIN task_groups ON task_groups.job_id = jobs.id "
            "WHERE jobs.state NOT IN ('completed', 'failed', 'needs_input', 'unknown') "
            "OR jobs.published_state != jobs.state "
            "ORDER BY CASE WHEN COALESCE(delivery_attempts.failures, 0) > 0 THEN 1 ELSE 0 END, jobs.rowid").fetchall()
        active_groups = {row["relay_group_id"] for row in rows if row["relay_group_id"] is not None and
                         (row["id"], "publish") in self.active}
        pending_groups = {row["relay_group_id"] for row in rows if row["relay_group_id"] is not None and
                          row["state"] not in TERMINAL_STATES and
                          json.loads(row["request_json"]).get("action") == "command.execute"}
        ready_api = (self.settings.api_results and allow_api_publication and
                     any(row["relay_group_id"] is not None and inline_batch_candidate(row) and
                         row["published_state"] != row["state"] and
                         (row["id"], "publish") not in self.active and time.time() >= row["delivery_retry_at"]
                         for row in rows))
        if ready_api:
            allow_git_publication = False
        grouped_publications: Dict[str, List[sqlite3.Row]] = {}
        self.next_batch_at = None
        agent_waiting = False
        for row in rows:
            task_id = row["id"]
            request = json.loads(row["request_json"])
            kind = "command" if request.get("action") == "command.execute" else "agent"
            execution_key, publication_key = (task_id, kind), (task_id, "publish")
            if kind == "agent" and execution_key in self.active:
                agent_waiting = True
            report_running = (kind == "command" and row["state"] == "running" and
                              request["command"].get("report_running", True))
            if row["state"] in TERMINAL_STATES or report_running:
                delivery_ready = time.time() >= row["delivery_retry_at"]
                if (allow_execution and publication_key not in self.active and
                        time.monotonic() >= self.retry_after.get(publication_key, 0) and
                        delivery_ready and row["published_state"] != row["state"] and self.capacity("publish")):
                    group_id = row["relay_group_id"]
                    if group_id is None:
                        if allow_git_publication:
                            self.launch(task_id, "publish", publication_worker)
                    elif group_id not in active_groups and (allow_git_publication or
                            (self.settings.api_results and allow_api_publication and inline_batch_candidate(row))):
                        grouped_publications.setdefault(group_id, []).append(row)
            if row["state"] in TERMINAL_STATES:
                continue
            if not allow_execution or row["state"] not in {"queued", "accepted"}:
                continue
            if execution_key in self.active:
                continue
            if kind == "agent" and agent_waiting:
                continue
            if time.monotonic() < self.retry_after.get(execution_key, 0):
                agent_waiting = agent_waiting or kind == "agent"
                continue
            if not self.capacity(kind):
                agent_waiting = agent_waiting or kind == "agent"
                continue
            if row["state"] == "queued":
                update_job(connection, task_id, "accepted", make_result(
                    row, "accepted", "Request accepted; execution has not started."))
            self.launch(task_id, kind, task_worker)
            agent_waiting = agent_waiting or kind == "agent"
        for group_id, group_rows in grouped_publications.items():
            if group_id in active_groups or not self.capacity("publish"):
                continue
            first = group_rows[0]
            candidates = [row for row in group_rows if inline_batch_candidate(row)][:8] if inline_batch_candidate(first) else []
            use_api = self.settings.api_results and allow_api_publication and bool(candidates)
            if len(candidates) > 1:
                self.launch_batch(group_id, [row["id"] for row in candidates], use_api)
            elif candidates and group_id in pending_groups:
                now = time.monotonic()
                deadline = self.batch_deadlines.setdefault(group_id, now + 0.4)
                if now < deadline:
                    self.next_batch_at = min(self.next_batch_at, deadline) if self.next_batch_at is not None else deadline
                    continue
                if use_api:
                    self.launch_batch(group_id, [first["id"]], True)
                else:
                    self.launch(first["id"], "publish", publication_worker)
            else:
                if use_api:
                    self.launch_batch(group_id, [first["id"]], True)
                else:
                    self.launch(first["id"], "publish", publication_worker)
            self.batch_deadlines.pop(group_id, None)
            active_groups.add(group_id)

    def capacity(self, kind: str) -> bool:
        maximum = {"command": self.settings.command_workers, "agent": 1,
                   "publish": self.settings.publish_workers}[kind]
        return len({worker.future for worker in self.active.values() if worker.kind == kind}) < maximum

    def api_inflight(self) -> bool:
        return any(worker.uses_api and not worker.future.done() for worker in self.active.values())

    def git_publication_inflight(self) -> bool:
        return any(worker.kind == "publish" and not worker.uses_api and not worker.future.done()
                   for worker in self.active.values())

    def launch(self, task_id: str, kind: str, worker: Any) -> None:
        future = self.executor.submit(worker, self.settings, task_id)
        self.active[(task_id, kind)] = ActiveWorker(future, kind)
        future.add_done_callback(lambda _future: self.wakeup.set())

    def launch_batch(self, group_id: str, task_ids: Sequence[str], use_api: bool = False) -> None:
        worker = api_batch_publication_worker if use_api else batch_publication_worker
        future = self.executor.submit(worker, self.settings, task_ids, group_id)
        for task_id in task_ids:
            self.active[(task_id, "publish")] = ActiveWorker(future, "publish", use_api)
        future.add_done_callback(lambda _future: self.wakeup.set())


def concurrent_sync(connection: sqlite3.Connection, git: GitTransport, settings: Settings) -> bool:
    before_fetch = metadata(connection, "results_head")
    started = time.monotonic()
    request_head, result_head = git.branch_tips((settings.requests_branch, settings.results_branch))
    LOGGER.info("Git branch synchronization took %.3f seconds", time.monotonic() - started)
    if not record_fetched_results_head(connection, git, result_head, before_fetch):
        return False
    ingest_requests(connection, git, settings, request_head)
    return True


def fetch_service_heads(settings: Settings) -> Tuple[str, str, float]:
    """Fetch remote heads without touching the service's SQLite connection."""
    git = GitTransport(settings)
    started = time.monotonic()
    request_head, result_head = git.branch_tips((settings.requests_branch, settings.results_branch))
    return request_head, result_head, time.monotonic() - started


class BackgroundSync:
    """Run one network fetch at a time while the dispatcher handles known jobs."""

    def __init__(self, settings: Settings, wakeup: threading.Event) -> None:
        self.settings = settings
        self.wakeup = wakeup
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future: Optional[concurrent.futures.Future] = None
        self.before_fetch: Optional[str] = None
        self.before_generation = "0"

    def start(self, connection: sqlite3.Connection) -> None:
        if self.future is not None:
            raise BridgeError("a background synchronization is already running")
        self.before_fetch = metadata(connection, "results_head")
        self.before_generation = result_generation(connection)
        self.future = self.executor.submit(fetch_service_heads, self.settings)
        self.future.add_done_callback(lambda _future: self.wakeup.set())

    def ready(self) -> bool:
        return self.future is not None and self.future.done()

    def finish(self, connection: sqlite3.Connection, git: GitTransport) -> bool:
        if self.future is None or not self.future.done():
            raise BridgeError("background synchronization is not ready")
        future = self.future
        self.future = None
        request_head, result_head, elapsed = future.result()
        LOGGER.info("Git branch synchronization took %.3f seconds", elapsed)
        if result_generation(connection) != self.before_generation:
            LOGGER.info("Atomic publication raced with synchronization; rechecking the result branch")
            return False
        if not record_fetched_results_head(connection, git, result_head, self.before_fetch):
            return False
        ingest_requests(connection, git, self.settings, request_head)
        return verify_atomic_generation(connection, self.before_generation)

    def close(self) -> None:
        self.executor.shutdown(wait=True)


def concurrent_service_loop_background(connection: sqlite3.Connection, git: GitTransport,
                                       settings: Settings) -> None:
    """Keep dispatching previously validated jobs during a network fetch."""
    dispatcher = ConcurrentDispatcher(settings)
    sync = BackgroundSync(settings, dispatcher.wakeup)
    watcher: Optional[GitHubRefWatcher] = None
    next_reconcile = 0.0
    next_poll_at = 0.0
    retry_watcher = 0.0
    watcher_backoff = 60
    synchronized = False
    sync_requested = False

    def request_sync() -> None:
        nonlocal sync_requested
        if dispatcher.api_inflight() or dispatcher.git_publication_inflight():
            sync_requested = True
        else:
            sync.start(connection)
            sync_requested = False

    try:
        while True:
            started = time.monotonic()
            try:
                dispatcher.reap(connection)
                if sync.ready():
                    synchronized = sync.finish(connection, git)
                    next_poll_at = time.monotonic() + settings.poll_seconds
                    next_reconcile = time.monotonic() + settings.reconcile_seconds if synchronized else 0.0
                if sync.future is None:
                    if sync_requested or not result_generation_verified(connection):
                        request_sync()
                    elif settings.watch_mode == "git":
                        if started >= next_poll_at:
                            request_sync()
                    elif started >= next_reconcile and started >= next_poll_at:
                        request_sync()
                    else:
                        if watcher is None and started >= retry_watcher:
                            try:
                                watcher = GitHubRefWatcher(git, settings)
                                LOGGER.info("GitHub ref watch active; interval %d seconds", settings.poll_seconds)
                            except (BridgeError, ValueError) as exc:
                                LOGGER.warning("GitHub ref watch unavailable; using Git polling: %s", type(exc).__name__)
                                retry_watcher = time.monotonic() + 60
                        if watcher is None:
                            if started >= next_poll_at:
                                request_sync()
                        elif started >= max(watcher.next_check_at, next_poll_at):
                            try:
                                head = watcher.check()
                            except BridgeError as exc:
                                LOGGER.warning("GitHub ref watch failed; using Git polling: %s", str(exc))
                                watcher.close()
                                watcher = None
                                retry_seconds = exc.retry_seconds if isinstance(exc, WatchRateLimit) else 60
                                if isinstance(exc, WatchRateLimit):
                                    retry_seconds = max(retry_seconds, watcher_backoff)
                                    watcher_backoff = min(3600, watcher_backoff * 2)
                                retry_watcher = time.monotonic() + retry_seconds
                                request_sync()
                            else:
                                next_poll_at = time.monotonic() + settings.poll_seconds
                                watcher_backoff = 60
                                if head != metadata(connection, "request_head"):
                                    LOGGER.info("GitHub request branch changed; synchronizing")
                                    request_sync()
            except (BridgeError, sqlite3.Error, OSError) as exc:
                connection.rollback()
                LOGGER.error("Concurrent sync failed; new tasks are paused: %s", exc)
                synchronized = False
                next_reconcile = 0.0
                next_poll_at = time.monotonic() + settings.poll_seconds
            except Exception as exc:  # Keep unexpected fetch failures from accepting new work.
                connection.rollback()
                LOGGER.error("Concurrent sync failed unexpectedly: %s", type(exc).__name__)
                synchronized = False
                next_reconcile = 0.0
                next_poll_at = time.monotonic() + settings.poll_seconds
            try:
                allow_git = (sync.future is None and not sync_requested and not dispatcher.api_inflight() and
                             result_generation_verified(connection))
                allow_api = (not sync_requested and not dispatcher.git_publication_inflight() and
                             atomic_api_available(connection))
                dispatcher.schedule(connection, synchronized, allow_git_publication=allow_git,
                                    allow_api_publication=allow_api)
            except (BridgeError, sqlite3.Error, OSError) as exc:
                connection.rollback()
                LOGGER.error("Concurrent scheduling failed: %s", type(exc).__name__)
                synchronized = False
                next_reconcile = 0.0
            now = time.monotonic()
            if sync.future is not None or sync_requested:
                delay = float(settings.poll_seconds)
            else:
                due = max(next_poll_at, watcher.next_check_at) if watcher is not None else next_poll_at
                delay = max(0.01, min(float(settings.poll_seconds), due - now))
                if settings.watch_mode == "github_api" and next_reconcile > 0:
                    delay = min(delay, max(0.01, next_reconcile - now))
            if dispatcher.next_batch_at is not None:
                delay = min(delay, max(0.01, dispatcher.next_batch_at - now))
            dispatcher.wakeup.wait(delay)
            dispatcher.wakeup.clear()
    finally:
        if watcher is not None:
            watcher.close()
        sync.close()
        dispatcher.close()


def concurrent_service_loop(connection: sqlite3.Connection, git: GitTransport, settings: Settings) -> None:
    dispatcher = ConcurrentDispatcher(settings)
    watcher: Optional[GitHubRefWatcher] = None
    next_reconcile = 0.0
    next_poll_at = 0.0
    retry_watcher = 0.0
    watcher_backoff = 60
    synchronized = False
    try:
        while True:
            started = time.monotonic()
            try:
                dispatcher.reap(connection)
                if settings.watch_mode == "git":
                    if started >= next_poll_at:
                        synchronized = concurrent_sync(connection, git, settings)
                        next_poll_at = time.monotonic() + settings.poll_seconds
                elif started >= next_reconcile and started >= next_poll_at:
                    synchronized = concurrent_sync(connection, git, settings)
                    next_reconcile = time.monotonic() + settings.reconcile_seconds if synchronized else 0.0
                else:
                    if watcher is None and started >= retry_watcher:
                        try:
                            watcher = GitHubRefWatcher(git, settings)
                            LOGGER.info("GitHub ref watch active; interval %d seconds", settings.poll_seconds)
                        except (BridgeError, ValueError) as exc:
                            LOGGER.warning("GitHub ref watch unavailable; using Git polling: %s", type(exc).__name__)
                            retry_watcher = time.monotonic() + 60
                    if watcher is None:
                        if started >= next_poll_at:
                            synchronized = concurrent_sync(connection, git, settings)
                            next_poll_at = time.monotonic() + settings.poll_seconds
                            next_reconcile = time.monotonic() + settings.reconcile_seconds if synchronized else 0.0
                    elif started >= max(watcher.next_check_at, next_poll_at):
                        try:
                            head = watcher.check()
                        except BridgeError as exc:
                            LOGGER.warning("GitHub ref watch failed; using Git polling: %s", str(exc))
                            watcher.close()
                            watcher = None
                            retry_seconds = exc.retry_seconds if isinstance(exc, WatchRateLimit) else 60
                            if isinstance(exc, WatchRateLimit):
                                retry_seconds = max(retry_seconds, watcher_backoff)
                                watcher_backoff = min(3600, watcher_backoff * 2)
                            retry_watcher = time.monotonic() + retry_seconds
                            synchronized = concurrent_sync(connection, git, settings)
                            next_poll_at = time.monotonic() + settings.poll_seconds
                            next_reconcile = time.monotonic() + settings.reconcile_seconds if synchronized else 0.0
                        else:
                            next_poll_at = time.monotonic() + settings.poll_seconds
                            watcher_backoff = 60
                            if head != metadata(connection, "request_head"):
                                LOGGER.info("GitHub request branch changed; synchronizing")
                                synchronized = concurrent_sync(connection, git, settings)
                                next_reconcile = time.monotonic() + settings.reconcile_seconds if synchronized else 0.0
            except (BridgeError, sqlite3.Error, OSError) as exc:
                connection.rollback()
                LOGGER.error("Concurrent sync failed; new tasks are paused: %s", exc)
                synchronized = False
                next_reconcile = 0.0
                next_poll_at = time.monotonic() + settings.poll_seconds
            try:
                dispatcher.schedule(connection, synchronized)
            except (BridgeError, sqlite3.Error, OSError) as exc:
                connection.rollback()
                LOGGER.error("Concurrent scheduling failed: %s", type(exc).__name__)
                synchronized = False
                next_reconcile = 0.0
            now = time.monotonic()
            due = max(next_poll_at, watcher.next_check_at) if watcher is not None else next_poll_at
            delay = max(0.01, min(float(settings.poll_seconds), due - now))
            if settings.watch_mode == "github_api" and next_reconcile > 0:
                delay = min(delay, max(0.01, next_reconcile - now))
            if dispatcher.next_batch_at is not None:
                delay = min(delay, max(0.01, dispatcher.next_batch_at - now))
            dispatcher.wakeup.wait(delay)
            dispatcher.wakeup.clear()
    finally:
        if watcher is not None:
            watcher.close()
        dispatcher.close()


def service_loop(connection: sqlite3.Connection, git: GitTransport, settings: Settings, once: bool) -> None:
    watcher: Optional[GitHubRefWatcher] = None
    next_reconcile = 0.0
    retry_watcher = 0.0
    watcher_backoff = 60
    try:
        while True:
            started = time.monotonic()
            try:
                if once or settings.watch_mode == "git":
                    cycle(connection, git, settings)
                elif started >= next_reconcile or pending_jobs(connection):
                    cycle(connection, git, settings)
                    next_reconcile = time.monotonic() + settings.reconcile_seconds
                else:
                    if watcher is None and started >= retry_watcher:
                        try:
                            watcher = GitHubRefWatcher(git, settings)
                            LOGGER.info("GitHub ref watch active; interval %d seconds", settings.poll_seconds)
                        except (BridgeError, ValueError) as exc:
                            LOGGER.warning("GitHub ref watch unavailable; using Git polling: %s", type(exc).__name__)
                            retry_watcher = time.monotonic() + 60
                    if watcher is None:
                        cycle(connection, git, settings)
                        next_reconcile = time.monotonic() + settings.reconcile_seconds
                    elif started >= watcher.next_check_at:
                        try:
                            head = watcher.check()
                        except BridgeError as exc:
                            LOGGER.warning("GitHub ref watch failed; using Git polling: %s", str(exc))
                            watcher.close()
                            watcher = None
                            retry_seconds = exc.retry_seconds if isinstance(exc, WatchRateLimit) else 60
                            if isinstance(exc, WatchRateLimit):
                                retry_seconds = max(retry_seconds, watcher_backoff)
                                watcher_backoff = min(3600, watcher_backoff * 2)
                            retry_watcher = time.monotonic() + retry_seconds
                            cycle(connection, git, settings)
                            next_reconcile = time.monotonic() + settings.reconcile_seconds
                        else:
                            watcher_backoff = 60
                            if head != metadata(connection, "request_head"):
                                LOGGER.info("GitHub request branch changed; synchronizing")
                                cycle(connection, git, settings)
                                next_reconcile = time.monotonic() + settings.reconcile_seconds
            except (BridgeError, sqlite3.Error, OSError) as exc:
                if once:
                    raise
                LOGGER.error("Cycle failed; will retry without rerunning started tasks: %s", exc)
                next_reconcile = 0.0
            if once:
                return
            delay = settings.poll_seconds if settings.watch_mode == "git" else max(
                0.0, settings.poll_seconds - (time.monotonic() - started))
            time.sleep(delay)
    finally:
        if watcher is not None:
            watcher.close()


def serve(settings: Settings, once: bool) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    file_log = RotatingFileHandler(str(settings.state_dir / "service.log"), maxBytes=5242880,
                                   backupCount=3, encoding="utf-8")
    file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    previous_log_level = LOGGER.level
    warning_filter = RepeatedWarningFilter()
    LOGGER.setLevel(TASK_LOG_LEVEL)
    LOGGER.addFilter(warning_filter)
    LOGGER.addHandler(file_log)
    try:
        lock = acquire_service_lock(settings.state_dir / "service-lock.sqlite3")
        try:
            LOGGER.info("Service lock acquired")
            connection = connect_database(settings.state_dir / "state.sqlite3")
            try:
                git = GitTransport(settings)
                recover_interrupted(connection)
                if settings.concurrent_tasks and not once:
                    if settings.background_sync:
                        concurrent_service_loop_background(connection, git, settings)
                    else:
                        concurrent_service_loop(connection, git, settings)
                else:
                    service_loop(connection, git, settings, once)
            finally:
                connection.close()
        finally:
            lock.close()
    finally:
        LOGGER.removeHandler(file_log)
        LOGGER.removeFilter(warning_filter)
        file_log.close()
        LOGGER.setLevel(previous_log_level)


def submit(settings: Settings, request_path: Path, fast: bool = False,
           group_id: Optional[str] = None) -> Dict[str, str]:
    try:
        data = request_path.read_bytes()
    except OSError as exc:
        raise BridgeError("cannot read request file") from exc
    request = validate_request(data)
    if fast and (request["action"] != "command.execute" or request["command"].get("report_running", True)):
        raise BridgeError("fast submit requires a direct command with report_running false")
    normalized = json_bytes(request)
    git = ConcurrentGitTransport(settings)
    cache_ref = request_cache_ref(git, settings)
    used_api = False
    if settings.api_submit and fast and group_id is not None:
        commit, used_api = submit_group_fast(git, settings, cache_ref, group_id, [request])
    else:
        state = ClientRequestHeads(settings, git) if settings.api_submit else None
        base = (checked_request_base(git, settings, cache_ref, state) if state is not None else
                cached_request_base(git, settings, cache_ref))
        if group_id is None:
            commit = git.publish(settings.requests_branch, REQUEST_PREFIX + request["id"] + ".json",
                                 normalized, immutable=True, base_hint=base, confirm_remote=not fast)
        else:
            commit = git.append_group_request(group_id, request, base_hint=base, confirm_remote=not fast)
        if state is not None:
            state.record(commit)
    if not used_api:
        try:
            remember_request_commit(git, cache_ref, commit)
        except BridgeError:
            LOGGER.warning("Could not update the local request cache")
    response = {"id": request["id"], "request_branch_commit": commit,
                "request_sha256": hashlib.sha256(normalized).hexdigest()}
    if group_id is not None:
        response["task_id"] = group_id
    return response


def submit_batch(settings: Settings, request_path: Path, group_id: str, fast: bool = False) -> Dict[str, Any]:
    try:
        data = request_path.read_bytes()
    except OSError as exc:
        raise BridgeError("cannot read request batch") from exc
    if len(data) > MAX_GROUP_BYTES:
        raise BridgeError("request batch exceeds size limit")
    batch = parse_json(data, "request batch")
    requests = batch.get("commands")
    if set(batch) != {"commands"} or not isinstance(requests, list) or not requests:
        raise BridgeError("request batch must contain a nonempty commands list")
    for request in requests:
        validate_request(json_bytes(request))
        if fast and (request["action"] != "command.execute" or request["command"].get("report_running", True)):
            raise BridgeError("fast submit requires direct commands with report_running false")
    git = ConcurrentGitTransport(settings)
    cache_ref = request_cache_ref(git, settings)
    used_api = False
    if settings.api_submit and fast:
        commit, used_api = submit_group_fast(git, settings, cache_ref, group_id, requests)
    else:
        state = ClientRequestHeads(settings, git) if settings.api_submit else None
        base = (checked_request_base(git, settings, cache_ref, state) if state is not None else
                cached_request_base(git, settings, cache_ref))
        commit = git.append_group_requests(group_id, requests, base_hint=base, confirm_remote=not fast)
        if state is not None:
            state.record(commit)
    if not used_api:
        try:
            remember_request_commit(git, cache_ref, commit)
        except BridgeError:
            LOGGER.warning("Could not update the local request cache")
    return {"task_id": group_id, "request_branch_commit": commit,
            "commands": [{"id": request["id"], "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest()}
                         for request in requests]}


def read_result(git: GitTransport, head: str, task_id: str, group_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if group_id is None:
        data = git.blob(head, RESULT_PREFIX + task_id + ".json")
        return parse_json(data, "result") if data is not None else None
    group_id = validate_identifier(group_id, "task ID")
    data = git.blob(head, GROUP_RESULT_PREFIX + group_id + ".json", MAX_GROUP_BYTES)
    if data is None:
        return None
    group = parse_json(data, "task result file")
    if group.get("version") != 2 or group.get("task_id") != group_id or not isinstance(group.get("results"), dict):
        raise BridgeError("invalid task result file")
    result = group["results"].get(task_id)
    if result is not None and not isinstance(result, dict):
        raise BridgeError("invalid command result in task file")
    return result


def status(settings: Settings, task_id: str, group_id: Optional[str] = None) -> Dict[str, Any]:
    task_id = validate_identifier(task_id, "id")
    git = GitTransport(settings)
    head = git.branch_tip(settings.results_branch)
    result = read_result(git, head, task_id, group_id)
    if result is None:
        return {"id": task_id, "state": "not_found", "results_branch_commit": head}
    result["results_branch_commit"] = head
    return result


def download_command_output(settings: Settings, task_id: str, stream: str, destination: Path,
                            results_commit: Optional[str] = None,
                            group_id: Optional[str] = None) -> Dict[str, Any]:
    task_id = validate_identifier(task_id, "id")
    git = GitTransport(settings)
    head = git.commit(results_commit) if results_commit is not None else git.branch_tip(settings.results_branch)
    result = read_result(git, head, task_id, group_id)
    if result is None:
        raise BridgeError("command result was not found")
    command_result = result.get("command_result")
    if not isinstance(command_result, dict) or not isinstance(command_result.get(stream), dict):
        raise BridgeError("result does not contain command output")
    info = command_result[stream]
    parts = info.get("parts")
    if not isinstance(parts, list):
        raise BridgeError("command output manifest is invalid")
    output_id = group_job_id(group_id, task_id) if group_id is not None else task_id
    digest = hashlib.sha256()
    size = 0
    try:
        output = destination.open("xb")
    except FileExistsError as exc:
        raise BridgeError("output destination already exists") from exc
    try:
        with output:
            if info.get("inline_base64") is not None:
                data = decode_inline_output(info)
                output.write(data)
                digest.update(data)
                size += len(data)
            for index, part in enumerate(parts):
                expected_path = OUTPUT_PREFIX + output_id + "/" + stream + ".%06d" % index
                if not isinstance(part, dict) or part.get("path") != expected_path:
                    raise BridgeError("command output manifest has an invalid path")
                chunk = git.blob(head, expected_path, COMMAND_CHUNK_BYTES)
                if chunk is None or len(chunk) != part.get("size") or hashlib.sha256(chunk).hexdigest() != part.get("sha256"):
                    raise BridgeError("command output part failed verification")
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != info.get("size") or digest.hexdigest() != info.get("sha256"):
            raise BridgeError("command output failed verification")
    except (BridgeError, OSError):
        destination.unlink(missing_ok=True)
        raise
    return {"id": task_id, "stream": stream, "output": str(destination), "size": size,
            "sha256": digest.hexdigest(), "results_branch_commit": head}


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue/green Git bridge")
    parser.add_argument("--config", required=True, help="Local JSON configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("serve", help="Run the green worker")
    worker.add_argument("--once", action="store_true", help="Run one polling cycle")
    sender = commands.add_parser("submit", help="Publish one immutable request")
    sender.add_argument("request_file")
    sender.add_argument("--fast", action="store_true", help="Use push acknowledgement for short direct commands")
    sender.add_argument("--task", help="Append the command to a stable task file")
    batch = commands.add_parser("submit-batch", help="Append several commands to one task in one commit")
    batch.add_argument("request_file")
    batch.add_argument("--task", required=True)
    batch.add_argument("--fast", action="store_true", help="Use push acknowledgement for short direct commands")
    reader = commands.add_parser("status", help="Read one result")
    reader.add_argument("task_id")
    reader.add_argument("--task", help="Read the command from a task result file")
    output = commands.add_parser("output", help="Download one raw command output stream")
    output.add_argument("task_id")
    output.add_argument("--stream", required=True, choices=("stdout", "stderr"))
    output.add_argument("--output", required=True, help="New local output path")
    output.add_argument("--results-commit", help="Already-fetched result commit from status; avoids a new fetch")
    output.add_argument("--task", help="Read the command from a task result file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = load_settings(Path(args.config))
        if args.command == "serve":
            serve(settings, args.once)
        elif args.command == "submit":
            sys.stdout.buffer.write(json_bytes(submit(settings, Path(args.request_file), args.fast, args.task)))
        elif args.command == "submit-batch":
            sys.stdout.buffer.write(json_bytes(submit_batch(settings, Path(args.request_file), args.task, args.fast)))
        elif args.command == "status":
            sys.stdout.buffer.write(json_bytes(status(settings, args.task_id, args.task)))
        else:
            sys.stdout.buffer.write(json_bytes(download_command_output(settings, args.task_id, args.stream,
                                                                       Path(args.output), args.results_commit, args.task)))
    except (BridgeError, sqlite3.Error, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
