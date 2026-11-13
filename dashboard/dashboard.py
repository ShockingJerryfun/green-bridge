#!/usr/bin/env python3
"""Blue/green collaboration dashboard.

Read-only viewer for the green-bridge transfer repository. It keeps its own
bare clone, never writes to the transfer repo, and never touches the blue
client's working clone or state directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SKILL_DIR = Path(__file__).resolve().parent.parent
MAX_TEXT_BYTES = 128 * 1024
MAX_NOTE_BYTES = 2000
MAX_NOTES = 500

SSH_CONTROL_PATH = Path.home() / ".ssh" / "green-bridge-%r@%h:%p"


def _git_env() -> dict:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    # ControlMaster is Unix-OpenSSH only; skip it on Windows and without ~/.ssh.
    if os.name != "nt" and (Path.home() / ".ssh").is_dir():
        env["GIT_SSH_COMMAND"] = (
            "ssh -o ControlMaster=auto -o ControlPersist=120 "
            "-o ControlPath=" + str(SSH_CONTROL_PATH)
        )
    return env


GIT_ENV = _git_env()

STATE_LABELS = {
    "queued": "排队中",
    "accepted": "已接收",
    "running": "执行中",
    "completed": "已完成",
    "failed": "失败",
    "needs_input": "待输入",
    "unknown": "未知",
    "delivery_pending": "回传中",
    "pending": "等待绿区",
}

SSH_VALUE_OPTIONS = {
    "-o", "-i", "-p", "-l", "-b", "-c", "-D", "-E", "-e", "-F", "-I",
    "-J", "-L", "-m", "-O", "-P", "-Q", "-R", "-S", "-W", "-w", "-B",
    "-pw", "--password",
}

BASE64_WRAPPER = re.compile(r"echo\s+([A-Za-z0-9+/=]{8,})\s*\|\s*base64\s+-d", re.S)


def mask_argv(argv: list[str]) -> list[str]:
    masked = []
    secret = False
    for value in argv:
        if secret:
            masked.append("***")
            secret = False
            continue
        masked.append(value)
        if value in ("-pw", "--password"):
            secret = True
    return masked


def mask_command(command):
    if not isinstance(command, dict):
        return command
    masked = dict(command)
    argv = masked.get("argv")
    if isinstance(argv, list):
        masked["argv"] = mask_argv([str(value) for value in argv])
    return masked


def decode_wrapper(text: str) -> tuple[str, list[str]]:
    """Decode `echo <base64> | base64 -d | bash` into the script it runs."""
    match = BASE64_WRAPPER.search(text)
    if match is None:
        return text, []
    try:
        decoded = base64.b64decode(match.group(1), validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeDecodeError):
        return text, []
    decoded = decoded.strip()
    if decoded.startswith("#!"):
        parts = decoded.split("\n", 1)
        decoded = parts[1].lstrip("\n") if len(parts) > 1 else ""
    targets = re.findall(r"(?:ssh|plink)(?:\.exe)?\b[^\n]*?([\w.-]+@[\w.-]+)", decoded)
    return decoded.strip(), targets


def split_ssh(argv: list[str]) -> tuple[str | None, list[str]]:
    index = 1
    target = None
    while index < len(argv):
        value = argv[index]
        if value == "--":
            index += 1
            break
        if value.startswith("-") and value != "-":
            index += 2 if value in SSH_VALUE_OPTIONS else 1
            continue
        target = value
        index += 1
        break
    return target, argv[index:]


def session_id(host: str, user: str) -> str:
    return hashlib.sha256(f"{user}@{host}".encode("utf-8")).hexdigest()[:12]


STATE_TEMPLATE = r"""D="$HOME/.green-bridge"
F="$D/cwd-__ID__"
STEP="$D/step-$$"
mkdir -p "$D"
chmod 700 "$D" 2>/dev/null || true
if [ -f "$F" ]; then cd "$(cat "$F")" 2>/dev/null || cd "$HOME"; else cd "$HOME"; fi
base64 -d > "$STEP" <<'GB_B64'
__PAYLOAD__
GB_B64
trap 'rc=$?; printf "%s" "$PWD" > "$F"; rm -f "$STEP"; exit $rc' EXIT
. "$STEP"
"""


def build_stateful_script(state: str, command: str) -> str:
    payload = base64.b64encode((command.rstrip() + "\n").encode("utf-8")).decode("ascii")
    return STATE_TEMPLATE.replace("__ID__", state).replace("__PAYLOAD__", payload)


def describe_command(request: dict) -> dict:
    state = request.get("shell_state")
    if isinstance(state, dict):
        argv = (request.get("command") or {}).get("argv") or []
        return {
            "type": "stateful",
            "target": state.get("target"),
            "targets": [],
            "effective": str(state.get("command") or ""),
            "transport": " ".join(mask_argv([str(value) for value in argv])),
        }
    command = request.get("command") or {}
    mode = command.get("mode")
    if mode == "powershell":
        script = (command.get("script") or "").strip()
        return {"type": "powershell", "target": None, "targets": [],
                "effective": script, "transport": script}
    argv = command.get("argv") or []
    if not argv:
        return {"type": "none", "target": None, "targets": [], "effective": "", "transport": ""}
    executable = os.path.basename(argv[0]).lower()
    if executable in ("bash", "sh") and len(argv) >= 3 and argv[1] in ("-lc", "-c"):
        decoded, targets = decode_wrapper(argv[2])
        return {"type": "local-script", "target": None, "targets": targets,
                "effective": decoded, "transport": " ".join(mask_argv(argv))}
    if executable in ("ssh", "ssh.exe", "plink", "plink.exe"):
        target, remote = split_ssh(argv)
        remote_text = " ".join(remote)
        decoded, targets = decode_wrapper(remote_text)
        if decoded != remote_text:
            effective = decoded
        elif len(remote) >= 3 and remote[0] in ("bash", "sh") and remote[1] in ("-lc", "-c"):
            effective = remote[2]
        else:
            effective = remote_text
        return {"type": "ssh", "target": target, "targets": targets,
                "effective": effective.strip(), "transport": " ".join(mask_argv(argv))}
    return {"type": "argv", "target": None, "targets": [],
            "effective": " ".join(mask_argv(argv)), "transport": " ".join(mask_argv(argv))}


def command_summary(request: dict, effective: dict) -> str:
    description = request.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()[:200]
    body = (effective.get("effective") or "").strip()
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    headline = ""
    for line in lines:
        match = re.search(r"echo\s+[\"']?(?:=+\s*)?\[?([^\"']{3,120})", line)
        if match:
            text = re.sub(r"^[=\-\s\[]+|[=\-\s\]]+$", "", match.group(1)).strip()
            text = re.sub(r"^\d+\]\s*", "", text)
            if text:
                headline = text
                break
    if not headline:
        for line in lines:
            if line.startswith("#") or line.startswith("set ") or line.startswith("date "):
                continue
            headline = line
            break
    if not headline and lines:
        headline = lines[0]
    label = effective.get("target") or ""
    if label and headline:
        return f"{label} · {headline[:120]}"
    return headline[:160] or (request.get("action") or "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


SESSION_ROOT = Path.home() / ".kimi-code" / "sessions"


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def find_current_wire() -> Path | None:
    candidates = list(SESSION_ROOT.glob("*/session_*/agents/main/wire.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def session_context(limit: int = 6, char_budget: int = 6000) -> dict:
    """Read the most recent user/assistant messages without calling a model."""
    wire = find_current_wire()
    if wire is None:
        return {"session_id": None, "captured_at": now_iso(), "messages": []}
    entries: list[tuple[float, str, str]] = []
    assistant_last: dict[str, tuple[float, str]] = {}
    try:
        with wire.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "context.append_message":
                    message = event.get("message") or {}
                    if message.get("role") != "user":
                        continue
                    text = _content_text(message.get("content")).strip()
                    if not text or text.startswith("<system-reminder>"):
                        continue
                    entries.append((float(event.get("time") or 0), "user", text))
                elif kind == "context.append_loop_event":
                    payload = event.get("event") or {}
                    if payload.get("type") != "content.part":
                        continue
                    part = payload.get("part") or {}
                    if part.get("type") != "text":
                        continue
                    text = (part.get("text") or "").strip()
                    if not text:
                        continue
                    turn = str(payload.get("turnId"))
                    assistant_last[turn] = (float(event.get("time") or 0), text)
    except OSError:
        return {"session_id": None, "captured_at": now_iso(), "messages": []}

    for time_value, text in assistant_last.values():
        entries.append((time_value, "assistant", text))
    entries.sort(key=lambda item: item[0])
    selected = entries[-max(1, limit):]

    messages: list[dict] = []
    budget = char_budget
    for time_value, role, text in reversed(selected):
        if budget <= 0:
            break
        clipped = text[: min(len(text), 1200)]
        if len(clipped) > budget:
            clipped = clipped[:budget]
        budget -= len(clipped)
        messages.append({
            "role": role,
            "text": clipped,
            "at": datetime.fromtimestamp(time_value / 1000, timezone.utc).isoformat(timespec="seconds"),
        })
    messages.reverse()
    session_name = wire.parents[2].name
    session_id = session_name[8:] if session_name.startswith("session_") else session_name
    return {"session_id": session_id, "captured_at": now_iso(), "messages": messages}


class GitError(RuntimeError):
    pass


def git(git_dir: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        capture_output=True,
        timeout=timeout,
        env=dict(GIT_ENV),
    )
    if proc.returncode != 0:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(message or "git command failed: " + " ".join(args))
    return proc


class Monitor:
    def __init__(self, config_path: Path, monitor_git: Path, interval: int):
        self.config_path = config_path
        self.config = read_json(config_path)
        if not isinstance(self.config, dict):
            raise SystemExit(f"无法读取蓝区配置：{config_path}")
        self.git_dir = Path(self.config["git_dir"])
        self.requests_branch = self.config.get("requests_branch", "pingpong/requests")
        self.results_branch = self.config.get("results_branch", "pingpong/results")
        self.remote = self.config.get("remote", "origin")
        self.monitor_git = monitor_git
        self.interval = interval
        self.notes_path = SKILL_DIR / "local" / "dashboard-notes.json"

        self.lock = threading.Lock()
        self.fetch_lock = threading.Lock()
        self.stop = threading.Event()
        self.last_fetch = 0.0
        self.state = {
            "tasks": [],
            "activity": [],
            "synced_at": None,
            "error": None,
            "requests_commit": None,
            "results_commit": None,
            "requests_branch": self.requests_branch,
            "results_branch": self.results_branch,
        }
        self.output_cache: dict[tuple, bytes] = {}

    # ---------- git plumbing ----------

    def remote_url(self) -> str:
        proc = git(self.git_dir, "config", "--get", f"remote.{self.remote}.url")
        return proc.stdout.decode("utf-8", "replace").strip()

    def ensure_clone(self) -> None:
        url = self.remote_url()
        if (self.monitor_git / "HEAD").exists():
            try:
                current = git(self.monitor_git, "config", "--get", "remote.origin.url")
                if current.stdout.decode("utf-8", "replace").strip() != url:
                    git(self.monitor_git, "remote", "set-url", "origin", url)
            except GitError:
                git(self.monitor_git, "remote", "set-url", "origin", url)
            return
        self.monitor_git.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--bare", url, str(self.monitor_git)],
            capture_output=True,
            timeout=180,
            env=dict(GIT_ENV),
        )
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(message or "无法克隆监控仓库")

    def fetch(self) -> None:
        refspecs = [
            f"+refs/heads/{self.requests_branch}:refs/heads/{self.requests_branch}",
            f"+refs/heads/{self.results_branch}:refs/heads/{self.results_branch}",
        ]
        git(self.monitor_git, "fetch", "--prune", "origin", *refspecs, timeout=90)

    def head(self, branch: str) -> str | None:
        try:
            proc = git(self.monitor_git, "rev-parse", f"refs/heads/{branch}")
            return proc.stdout.decode().strip()
        except GitError:
            return None

    def paths(self, branch: str) -> list[str]:
        proc = git(self.monitor_git, "ls-tree", "-r", "--name-only", f"refs/heads/{branch}")
        return proc.stdout.decode("utf-8", "replace").splitlines()

    def blob(self, branch: str, path: str) -> bytes | None:
        try:
            proc = git(self.monitor_git, "cat-file", "blob", f"refs/heads/{branch}:{path}")
            return proc.stdout
        except GitError:
            return None

    def json_blob(self, branch: str, path: str):
        data = self.blob(branch, path)
        if data is None:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def read_blobs(self, branch: str, paths: list[str]) -> dict[str, bytes | None]:
        """Read many blobs with one git process instead of one per file."""
        if not paths:
            return {}
        ref = f"refs/heads/{branch}"
        payload = b"".join(f"{ref}:{path}\n".encode("utf-8") for path in paths)
        proc = subprocess.run(
            ["git", "--git-dir", str(self.monitor_git), "cat-file", "--batch"],
            input=payload, capture_output=True, timeout=180,
            env=dict(GIT_ENV),
        )
        if proc.returncode != 0:
            message = proc.stderr.decode("utf-8", "replace").strip()
            raise GitError(message or "git cat-file --batch failed")
        data = proc.stdout
        result: dict[str, bytes | None] = {}
        pos = 0
        for path in paths:
            newline = data.find(b"\n", pos)
            if newline < 0:
                result[path] = None
                continue
            header = data[pos:newline].decode("utf-8", "replace")
            pos = newline + 1
            parts = header.split()
            if len(parts) != 3:
                result[path] = None
                continue
            size = int(parts[2])
            result[path] = data[pos:pos + size]
            pos += size + 1
        return result

    def file_times(self, branch: str, limit: int = 5000) -> dict[str, str]:
        """Map each path to the commit time that last touched it, in one pass."""
        try:
            proc = git(
                self.monitor_git, "log", f"-n{limit}", "--format=@@%cI",
                "--name-only", f"refs/heads/{branch}",
            )
        except GitError:
            return {}
        times: dict[str, str] = {}
        current = None
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("@@"):
                current = line[2:].strip()
            elif line.strip() and current:
                times.setdefault(line.strip(), current)
        return times

    def log_entries(self, branch: str, limit: int = 30) -> list[dict]:
        try:
            proc = git(
                self.monitor_git, "log", f"-n{limit}", "--date=iso-strict",
                "--format=%H%x1f%an%x1f%aI%x1f%s", f"refs/heads/{branch}",
            )
        except GitError:
            return []
        entries = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                entries.append({
                    "commit": parts[0], "author": parts[1], "date": parts[2],
                    "subject": parts[3], "branch": branch,
                })
        return entries

    # ---------- collection ----------

    def collect(self) -> dict:
        tasks: dict[str, dict] = {}
        requests_branch = self.requests_branch
        results_branch = self.results_branch

        req_group_prefix = "pingpong/task-requests/"
        res_group_prefix = "pingpong/task-results/"
        req_flat_prefix = "pingpong/requests/"
        res_flat_prefix = "pingpong/results/"

        def task(tid: str, kind: str) -> dict:
            entry = tasks.get(tid)
            if entry is None:
                entry = {
                    "id": tid, "kind": kind, "commands": {}, "results": {},
                    "created_at": None, "updated_at": None,
                }
                tasks[tid] = entry
            return entry

        req_paths = [
            path for path in self.paths(requests_branch)
            if (path.startswith(req_group_prefix) or path.startswith(req_flat_prefix))
            and path.endswith(".json")
        ]
        res_paths = [
            path for path in self.paths(results_branch)
            if (path.startswith(res_group_prefix) or path.startswith(res_flat_prefix))
            and path.endswith(".json")
        ]
        req_blobs = self.read_blobs(requests_branch, req_paths)
        res_blobs = self.read_blobs(results_branch, res_paths)
        req_times = self.file_times(requests_branch)

        def parse(raw):
            if raw is None:
                return None
            try:
                value = json.loads(raw.decode("utf-8"))
                return value if isinstance(value, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None

        for path in req_paths:
            if path.startswith(req_group_prefix):
                tid = path[len(req_group_prefix):-5]
                data = parse(req_blobs.get(path))
                if data is None:
                    continue
                entry = task(tid, "group")
                for request in data.get("commands", []):
                    if isinstance(request, dict) and isinstance(request.get("id"), str):
                        entry["commands"][request["id"]] = request
                        entry["created_at"] = entry["created_at"] or req_times.get(path)
            else:
                tid = path[len(req_flat_prefix):-5]
                data = parse(req_blobs.get(path))
                if data is None:
                    continue
                entry = task(tid, "legacy")
                entry["commands"][tid] = data
                entry["created_at"] = req_times.get(path)

        for path in res_paths:
            if path.startswith(res_group_prefix):
                tid = path[len(res_group_prefix):-5]
                data = parse(res_blobs.get(path))
                if data is None:
                    continue
                entry = task(tid, "group")
                for cid, result in (data.get("results") or {}).items():
                    if isinstance(result, dict):
                        entry["results"][cid] = result
            else:
                tid = path[len(res_flat_prefix):-5]
                data = parse(res_blobs.get(path))
                if data is None:
                    continue
                entry = task(tid, "legacy")
                entry["results"][tid] = data

        req_head = self.head(requests_branch)
        res_head = self.head(results_branch)

        tasks_out = []
        for tid, entry in tasks.items():
            commands = []
            updated = entry["created_at"]
            for cid, request in entry["commands"].items():
                result = entry["results"].get(cid) or {}
                command_result = result.get("command_result") or {}
                timing = result.get("timing") or {}
                state = result.get("state") or "pending"
                if result.get("state") == "delivery_pending" and result.get("execution_state"):
                    state = "delivery_pending"
                effective = describe_command(request)
                commands.append({
                    "id": cid,
                    "description": request.get("description") or "",
                    "summary": command_summary(request, effective),
                    "effective": effective,
                    "action": request.get("action"),
                    "session": request.get("session"),
                    "timeout_seconds": request.get("timeout_seconds"),
                    "command": mask_command(request.get("command")),
                    "context": request.get("context"),
                    "shell_state": request.get("shell_state"),
                    "message": result.get("message") or "",
                    "state": state,
                    "state_label": STATE_LABELS.get(state, state),
                    "exit_code": command_result.get("exit_code"),
                    "timed_out": command_result.get("timed_out"),
                    "launch_error": command_result.get("launch_error"),
                    "duration_seconds": command_result.get("duration_seconds"),
                    "stdout_size": (command_result.get("stdout") or {}).get("size"),
                    "stderr_size": (command_result.get("stderr") or {}).get("size"),
                    "timing": timing,
                    "updated_at": result.get("updated_at"),
                })
                for stamp in (result.get("updated_at"), timing.get("finished_at"),
                              timing.get("running_at"), timing.get("accepted_at"),
                              timing.get("received_at"), timing.get("queued_at")):
                    if stamp and (updated is None or stamp > updated):
                        updated = stamp
            # results that arrived without a local request file still show up
            for cid, result in entry["results"].items():
                if cid in entry["commands"]:
                    continue
                commands.append({
                    "id": cid,
                    "description": result.get("description") or "",
                    "summary": result.get("description") or result.get("id") or cid,
                    "effective": {"type": "unknown", "target": None, "targets": [],
                                  "effective": "", "transport": ""},
                    "action": result.get("action"),
                    "session": result.get("session"),
                    "timeout_seconds": None,
                    "command": None,
                    "message": result.get("message") or "",
                    "state": result.get("state") or "unknown",
                    "state_label": STATE_LABELS.get(result.get("state") or "unknown", result.get("state") or "unknown"),
                    "exit_code": (result.get("command_result") or {}).get("exit_code"),
                    "timed_out": (result.get("command_result") or {}).get("timed_out"),
                    "launch_error": (result.get("command_result") or {}).get("launch_error"),
                    "duration_seconds": (result.get("command_result") or {}).get("duration_seconds"),
                    "stdout_size": ((result.get("command_result") or {}).get("stdout") or {}).get("size"),
                    "stderr_size": ((result.get("command_result") or {}).get("stderr") or {}).get("size"),
                    "timing": result.get("timing") or {},
                    "updated_at": result.get("updated_at"),
                })
            summary = {"total": len(commands), "completed": 0, "failed": 0,
                       "running": 0, "pending": 0, "unknown": 0, "delivery": 0}
            for item in commands:
                state = item["state"]
                if state == "completed":
                    summary["completed"] += 1
                elif state == "failed":
                    summary["failed"] += 1
                elif state in ("running", "accepted", "queued"):
                    summary["running"] += 1
                elif state == "delivery_pending":
                    summary["delivery"] += 1
                elif state == "unknown":
                    summary["unknown"] += 1
                else:
                    summary["pending"] += 1
            commands.sort(key=lambda item: item["id"])
            tasks_out.append({
                "id": tid, "kind": entry["kind"], "commands": commands,
                "summary": summary, "created_at": entry["created_at"],
                "updated_at": updated,
                "requests_commit": req_head,
                "results_commit": res_head,
            })

        tasks_out.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        activity = self.log_entries(requests_branch) + self.log_entries(results_branch)
        activity.sort(key=lambda item: item.get("date") or "", reverse=True)
        return {
            "tasks": tasks_out,
            "activity": activity[:40],
            "synced_at": now_iso(),
            "error": None,
            "requests_commit": req_head,
            "results_commit": res_head,
            "requests_branch": requests_branch,
            "results_branch": results_branch,
            "repo": str(self.monitor_git),
        }

    def compact_state(self) -> dict:
        """Small payload for polling: no commands, no scripts, no context."""
        with self.lock:
            tasks = []
            for task in self.state.get("tasks", []):
                commands = task.get("commands") or []
                tasks.append({
                    "id": task.get("id"),
                    "kind": task.get("kind"),
                    "created_at": task.get("created_at"),
                    "updated_at": task.get("updated_at"),
                    "summary": task.get("summary") or {},
                    "first_summary": commands[0].get("summary") if commands else "",
                    "command_count": len(commands),
                })
            return {
                "tasks": tasks,
                "activity": self.state.get("activity", []),
                "synced_at": self.state.get("synced_at"),
                "error": self.state.get("error"),
                "requests_commit": self.state.get("requests_commit"),
                "results_commit": self.state.get("results_commit"),
                "requests_branch": self.state.get("requests_branch"),
                "results_branch": self.state.get("results_branch"),
                "repo": self.state.get("repo"),
            }

    def version(self) -> dict:
        with self.lock:
            return {
                "requests_commit": self.state.get("requests_commit"),
                "results_commit": self.state.get("results_commit"),
                "synced_at": self.state.get("synced_at"),
                "error": self.state.get("error"),
                "task_count": len(self.state.get("tasks", [])),
            }

    def task_detail(self, task_id: str) -> dict | None:
        with self.lock:
            for task in self.state.get("tasks", []):
                if task.get("id") == task_id:
                    return task
        return None

    def recent_tasks(self, limit: int) -> list[dict]:
        with self.lock:
            return list(self.state.get("tasks", [])[:limit])

    # ---------- outputs ----------

    def output_bytes(self, task_id: str, command_id: str, stream: str) -> tuple[bytes, dict]:
        if stream not in ("stdout", "stderr"):
            raise ValueError("stream must be stdout or stderr")
        head = self.head(self.results_branch)
        if not head:
            raise ValueError("结果分支不存在")
        cache_key = (head, task_id, command_id, stream)
        if cache_key in self.output_cache:
            return self.output_cache[cache_key], {}

        with self.lock:
            task_entry = next((t for t in self.state["tasks"] if t["id"] == task_id), None)
        kind = task_entry["kind"] if task_entry else "group"

        if kind == "legacy":
            result = self.json_blob(self.results_branch, f"pingpong/results/{task_id}.json")
            output_id = task_id
        else:
            group = self.json_blob(self.results_branch, f"pingpong/task-results/{task_id}.json")
            result = (group or {}).get("results", {}).get(command_id)
            output_id = "g-" + hashlib.sha256((task_id + "\0" + command_id).encode()).hexdigest()

        if not isinstance(result, dict):
            raise ValueError("找不到该指令的结果")
        command_result = result.get("command_result") or {}
        info = command_result.get(stream)
        if not isinstance(info, dict):
            raise ValueError("该结果没有可读取的输出")

        digest = hashlib.sha256()
        data = bytearray()
        inline = info.get("inline_base64")
        if inline is not None:
            chunk = base64.b64decode(inline)
            data.extend(chunk)
            digest.update(chunk)
        for part in info.get("parts") or []:
            path = part.get("path") if isinstance(part, dict) else None
            if not isinstance(path, str):
                continue
            chunk = self.blob(self.results_branch, path)
            if chunk is None:
                raise ValueError("输出分段缺失")
            data.extend(chunk)
            digest.update(chunk)

        if info.get("size") is not None and len(data) != info["size"]:
            raise ValueError("输出长度与清单不一致")
        if info.get("sha256") and digest.hexdigest() != info["sha256"]:
            raise ValueError("输出校验失败")

        value = bytes(data)
        if len(self.output_cache) > 8:
            self.output_cache.clear()
        self.output_cache[cache_key] = value
        return value, info

    # ---------- notes ----------

    def notes(self) -> list[dict]:
        data = read_json(self.notes_path, [])
        return data if isinstance(data, list) else []

    def add_note(self, author: str, text: str) -> list[dict]:
        text = " ".join(text.split())
        if not text:
            raise ValueError("备注不能为空")
        if len(text) > MAX_NOTE_BYTES:
            raise ValueError("备注过长")
        author = " ".join(str(author or "本机").split())[:40] or "本机"
        notes = self.notes()
        notes.append({
            "author": author, "text": text, "created_at": now_iso(),
        })
        notes = notes[-MAX_NOTES:]
        self.notes_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.notes_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.notes_path)
        return notes

    def servers(self) -> list[dict]:
        data = read_json(SKILL_DIR / "local" / "servers.json", {}) or {}
        servers = data.get("servers") if isinstance(data, dict) else None
        clean = []
        for item in servers or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "")
            user = str(item.get("user") or "root")
            if re.fullmatch(r"[A-Za-z0-9._-]+", host) and re.fullmatch(r"[A-Za-z0-9._-]+", user):
                clean.append({
                    "label": str(item.get("label") or host),
                    "host": host,
                    "user": user,
                })
        return clean

    def compose(self, payload: dict) -> dict:
        host = str(payload.get("host") or "")
        user = str(payload.get("user") or "root")
        command = str(payload.get("command") or "")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            raise ValueError("目标地址无效")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", user):
            raise ValueError("用户无效")
        if not command.strip():
            raise ValueError("命令不能为空")
        if len(command) > 100000:
            raise ValueError("命令过长")

        config = read_json(SKILL_DIR / "local" / "servers.json", {}) or {}
        options = config.get("ssh_options") if isinstance(config, dict) else None
        if not isinstance(options, list) or not all(isinstance(v, str) for v in options):
            options = ["-T", "-n", "-o", "BatchMode=yes",
                       "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8"]
        cwd = config.get("cwd") if isinstance(config, dict) else None
        if not isinstance(cwd, str) or not cwd:
            cwd = "C:\\"
        timeout = config.get("default_timeout_seconds") if isinstance(config, dict) else None
        if not isinstance(timeout, int) or timeout < 1:
            timeout = 180

        script = command.rstrip() + "\n"
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        remote = f"echo {encoded} | base64 -d | bash"
        argv = ["ssh", *options, f"{user}@{host}", remote]
        first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
        task_id = "ui-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        request = {
            "version": 1,
            "id": task_id,
            "action": "command.execute",
            "description": f"在 {user}@{host} 执行：{first_line[:120]}",
            "command": {"mode": "argv", "cwd": cwd, "argv": argv},
            "timeout_seconds": timeout,
        }
        if payload.get("stateful") is True:
            state = session_id(host, user)
            state_script = build_stateful_script(state, command)
            state_encoded = base64.b64encode(state_script.encode("utf-8")).decode("ascii")
            request["command"] = {
                "mode": "argv",
                "cwd": cwd,
                "argv": ["ssh", *options, f"{user}@{host}",
                         f"echo {state_encoded} | base64 -d | bash"],
            }
            request["description"] = f"在 {user}@{host} 保持目录执行：{first_line[:100]}"
            request["shell_state"] = {
                "id": state,
                "target": f"{user}@{host}",
                "command": command,
            }
        note = str(payload.get("context_note") or "").strip()
        include_context = payload.get("include_context", True) is not False
        supplied = payload.get("messages")
        if isinstance(supplied, list) and supplied:
            messages = []
            for item in supplied[-8:]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                messages.append({
                    "role": "assistant" if item.get("role") == "assistant" else "user",
                    "text": text[:1200],
                    "at": str(item.get("at") or now_iso()),
                })
            context = {"source": "chat", "session_id": None,
                       "captured_at": now_iso(), "messages": messages}
        elif include_context or note:
            context = session_context(limit=6)
            context["source"] = "dashboard"
            if not include_context:
                context["messages"] = []
        else:
            context = None
        if context is not None:
            context["note"] = note[:2000]
            if context.get("messages") or context.get("note"):
                request["context"] = context
        return {"request": request, "preview": describe_command(request)}

    def save_outbox(self, request: dict) -> Path:
        if not isinstance(request, dict):
            raise ValueError("请求内容无效")
        task_id = request.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[a-z0-9._-]{1,128}", task_id):
            raise ValueError("任务 ID 无效")
        target = SKILL_DIR / "local" / "outbox" / f"{task_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(request, ensure_ascii=False, indent=2) + "\n"
        if target.exists():
            if target.read_text(encoding="utf-8") == content:
                return target
            raise ValueError("已存在同 ID 但内容不同的待派单文件")
        target.write_text(content, encoding="utf-8")
        return target

    def dispatch(self, request: dict) -> dict:
        """Save the request, then submit it through the blue client (writes to the transfer repo)."""
        path = self.save_outbox(request)
        proc = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "green_bridge.py"),
             "--config", str(self.config_path), "submit", str(path)],
            capture_output=True, text=True, timeout=180,
            env=dict(GIT_ENV),
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(detail or "派单失败")
        self.refresh(force=True)
        return {"path": str(path), "id": request.get("id"),
                "output": (proc.stdout or "").strip()}

    # ---------- refresh loop ----------

    def refresh(self, force: bool = False) -> None:
        if not self.fetch_lock.acquire(blocking=force):
            return
        try:
            if not force and time.time() - self.last_fetch < self.interval:
                return
            self.ensure_clone()
            self.fetch()
            new_req = self.head(self.requests_branch)
            new_res = self.head(self.results_branch)
            with self.lock:
                unchanged = (self.state.get("requests_commit") == new_req and
                             self.state.get("results_commit") == new_res and
                             self.state.get("error") is None)
            if unchanged and not force:
                self.last_fetch = time.time()
                return
            data = self.collect()
            with self.lock:
                self.state = data
            self.last_fetch = time.time()
        except Exception as exc:  # keep the last good snapshot visible
            with self.lock:
                self.state["error"] = f"{type(exc).__name__}: {exc}"
                self.state["synced_at"] = now_iso()
        finally:
            self.fetch_lock.release()

    def run(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            self.refresh()
            elapsed = time.monotonic() - started
            self.stop.wait(max(0.3, self.interval - elapsed))


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>蓝绿协作监控台</title>
<style>
:root{
  --bg:#f6f7f9; --panel:#fff; --ink:#1c2330; --muted:#6b7480; --line:#e4e8ee;
  --accent:#2f6df6; --ok:#1f9d5a; --warn:#c07a12; --bad:#d0453c; --gray:#8a929e;
  --chip:#eef2f8;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#12161c; --panel:#1a2029; --ink:#e7ecf3; --muted:#98a2b0; --line:#2a323d;
    --accent:#6f9bff; --ok:#43c07c; --warn:#e0a33e; --bad:#ff6f63; --gray:#8f98a6;
    --chip:#222a35;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 20px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{font-size:16px;font-weight:650;letter-spacing:.2px}
.brand small{font-weight:400;color:var(--muted);margin-left:8px;font-size:12px}
.status{margin-left:auto;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
.dot.bad{background:var(--bad)}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:5px 12px}
button:hover{border-color:var(--accent);color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;padding:16px 20px 0}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.stat b{display:block;font-size:24px;line-height:1.2}
.stat span{color:var(--muted);font-size:12px}
.layout{display:grid;grid-template-columns:300px minmax(0,1fr) 330px;gap:16px;padding:16px 20px 40px;align-items:start}
@media (max-width:1100px){.layout{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px}
.panel h2{font-size:13px;font-weight:650;margin:0;padding:13px 15px;border-bottom:1px solid var(--line);color:var(--muted);letter-spacing:.4px}
.tasks{max-height:calc(100vh - 220px);overflow:auto}
.task{padding:11px 14px;border-bottom:1px solid var(--line);cursor:pointer}
.task:last-child{border-bottom:0}
.task:hover{background:var(--chip)}
.task.active{background:var(--chip);box-shadow:inset 3px 0 0 var(--accent)}
.task .top{display:flex;align-items:center;gap:8px}
.task .id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.task .desc{font-size:13px;margin:4px 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task .time{font-size:11px;color:var(--muted)}
.chip{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--gray);white-space:nowrap}
.chip.ok{color:var(--ok)} .chip.bad{color:var(--bad)} .chip.run{color:var(--accent)} .chip.warn{color:var(--warn)}
.detail{padding:16px 18px;min-height:280px}
.detail h3{margin:0 0 4px;font-size:18px}
.meta{color:var(--muted);font-size:12px;margin-bottom:14px;word-break:break-all}
.cmd{border:1px solid var(--line);border-radius:10px;margin-bottom:12px;overflow:hidden}
.cmd summary{list-style:none;cursor:pointer;padding:10px 12px;display:flex;align-items:center;gap:10px;background:var(--chip)}
.cmd summary::-webkit-details-marker{display:none}
.cmd summary .t{font-weight:550;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmd .body{padding:12px}
.kv{display:grid;grid-template-columns:110px 1fr;gap:4px 10px;font-size:12px;margin-bottom:10px}
.kv span:nth-child(odd){color:var(--muted)}
pre.code{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;max-height:220px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;word-break:break-all}
.tabs{display:flex;gap:6px;margin:10px 0 6px}
.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.output{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;min-height:60px;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-all;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
.hint{color:var(--muted);font-size:12px}
.empty{padding:48px 24px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:16px;margin-bottom:6px}
.side .block{padding:14px 15px;border-bottom:1px solid var(--line)}
.side .block:last-child{border-bottom:0}
textarea{width:100%;min-height:74px;resize:vertical;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:8px;font:inherit}
.note{border-left:3px solid var(--accent);padding:4px 0 4px 10px;margin:10px 0;font-size:12px}
.note .who{color:var(--muted);font-size:11px}
.reserved{display:grid;gap:8px}
.rcard{border:1px dashed var(--line);border-radius:10px;padding:9px 11px;color:var(--muted);font-size:12px}
.rcard b{color:var(--ink);font-weight:550}
.activity{font-size:12px;max-height:220px;overflow:auto}
.activity div{padding:5px 0;border-bottom:1px dashed var(--line);color:var(--muted)}
.activity div:last-child{border-bottom:0}
.activity code{font-size:11px;color:var(--ink)}
.banner{margin:0 20px;padding:9px 12px;border-radius:10px;background:#fff4e5;color:#8a5a00;font-size:12px;border:1px solid #f0d7ac;display:none}
</style>
</head>
<body>
<header>
  <div class="brand">蓝绿协作监控台<small>Green Bridge · 只读</small></div>
  <div class="status"><span class="dot" id="dot"></span><span id="sync">等待首次同步…</span><a href="/chat" style="color:var(--accent);text-decoration:none">对话视图</a><button id="refresh">立即刷新</button></div>
</header>
<div class="banner" id="banner"></div>
<div class="stats">
  <div class="stat"><b id="s-tasks">0</b><span>任务数</span></div>
  <div class="stat"><b id="s-run">0</b><span>执行中</span></div>
  <div class="stat"><b id="s-ok">0</b><span>已完成</span></div>
  <div class="stat"><b id="s-bad">0</b><span>失败 / 未知</span></div>
</div>
<div class="layout">
  <aside class="panel tasks">
    <h2>任务列表 <span id="task-count" style="float:right;font-weight:400"></span></h2>
    <div style="padding:8px 12px;border-bottom:1px solid var(--line)">
      <input id="filter" placeholder="筛选任务 ID 或说明…" style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:6px 8px;color:var(--ink);font:inherit">
    </div>
    <div id="task-list"><div class="empty">暂无任务</div></div>
  </aside>
  <section class="panel detail" id="detail">
    <div class="empty"><b>还没有可展示的任务</b>蓝区下发指令后，这里会显示每条命令、执行状态和原始输出。</div>
  </section>
  <aside class="panel side">
    <h2>协同区</h2>
    <div class="block">
      <div class="hint" style="margin-bottom:8px">命令构造器：从绿区 SSH 到目标环境执行</div>
      <select id="env-select" style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:6px 8px;margin-bottom:8px"></select>
      <textarea id="compose-text" placeholder="只写远程要执行的命令，例如：npu-smi info"></textarea>
      <label class="hint" style="display:flex;align-items:center;gap:6px;margin-top:8px">
        <input type="checkbox" id="compose-ctx" checked> 附带最近对话上下文
      </label>
      <label class="hint" style="display:flex;align-items:center;gap:6px;margin-top:6px">
        <input type="checkbox" id="compose-stateful" checked> 保持目录（cd 后下一条仍在原目录；请等上一条完成）
      </label>
      <input id="compose-note" placeholder="补充说明（可选，随任务写入）" style="width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:6px 8px;margin-top:6px">
      <details style="margin-top:6px"><summary class="hint" style="cursor:pointer">预览将附带的上下文</summary><div id="ctx-preview" class="activity"></div></details>
      <div style="display:flex;gap:8px;margin-top:8px">
        <button id="compose-btn">生成命令</button>
        <button id="compose-save" disabled>保存待派单</button>
      </div>
      <div id="compose-result"></div>
    </div>
    <div class="block">
      <div class="hint" style="margin-bottom:8px">本机备注，不会写入传输仓库。</div>
      <textarea id="note-text" placeholder="写下判读结论、待确认事项或给绿区的说明…"></textarea>
      <div style="display:flex;gap:8px;margin-top:8px">
        <input id="note-author" value="我" style="width:70px;background:var(--bg);border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:6px 8px">
        <button id="note-add">添加备注</button>
      </div>
      <div id="notes"></div>
    </div>
    <div class="block">
      <div class="hint" style="margin-bottom:8px">预留位置</div>
      <div class="reserved">
        <div class="rcard"><b>人工审批</b><br>执行前确认 / 白名单（待接入）</div>
        <div class="rcard"><b>多绿区调度</b><br>按机器分流与负载视图（待接入）</div>
        <div class="rcard"><b>任务模板</b><br>常用只读检查一键派单（待接入）</div>
      </div>
    </div>
    <div class="block">
      <div class="hint" style="margin-bottom:6px">提交动态</div>
      <div class="activity" id="activity"></div>
    </div>
  </aside>
</div>
<script>
const state = { tasks: [], selected: null, output: {}, tab: {}, openCmds: {}, followTask: null, lastSig: '', detail: null, detailLoading: null };
const LABELS = %LABELS%;

function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function chipClass(s){
  if(s==='completed')return 'ok';
  if(s==='failed')return 'bad';
  if(s==='running'||s==='accepted'||s==='queued')return 'run';
  if(s==='unknown'||s==='delivery_pending')return 'warn';
  return '';
}
function fmtTime(v){
  if(!v)return '—';
  const d=new Date(v);
  if(isNaN(d))return v;
  return d.toLocaleString('zh-CN',{hour12:false});
}
function fmtDur(v){ return (v===null||v===undefined)?'—':Number(v).toFixed(2)+' s'; }
function bytes(n){ if(n===null||n===undefined)return '—'; if(n<1024)return n+' B'; if(n<1048576)return (n/1024).toFixed(1)+' KB'; return (n/1048576).toFixed(2)+' MB'; }
function humanMessage(m){
  if(!m)return '—';
  const map={
    'Local command exited; output is captured without decoding.':'命令已结束，输出按原始字节保存。',
    'Command timed out; captured output is partial and child processes may still be running.':'命令超时，输出为不完整快照，子进程可能仍在运行。',
    'Command could not be started.':'命令无法启动。',
    'Service stopped during execution; no automatic retry.':'服务在执行中停止；不会自动重跑。',
    'Starting adapter; interruption may leave an unknown outcome.':'正在启动代理；中断可能导致结果未知。'
  };
  return map[m]||m;
}
function actionLabel(a){
  const map={'command.execute':'直接执行命令','task.execute':'代理任务','session.new':'新建会话',
    'session.attach':'接管会话','session.switch':'切换会话','session.checkpoint':'阶段摘要'};
  return map[a]||a||'—';
}
function cmdPreview(c){
  if(!c)return '';
  if(c.command&&c.command.mode==='powershell'){
    const line=(c.command.script||'').split('\n').map(x=>x.trim()).find(Boolean);
    return line?line.slice(0,80):'PowerShell';
  }
  if(c.command&&c.command.argv&&c.command.argv.length){
    const argv=c.command.argv;
    if(argv[0]==='ssh'){
      const host=argv.find(a=>/^[\w.-]+@[\w.-]+$/.test(a));
      return 'SSH '+(host||'远程')+(argv.includes('bash')?'（远程脚本）':'');
    }
    return argv.join(' ').slice(0,80);
  }
  return c.action||c.id||'';
}

async function load(){
  try{
    const res=await fetch('/api/state',{cache:'no-store'});
    const data=await res.json();
    state.tasks=data.tasks||[];
    let followHit=false;
    if(state.followTask){
      const target=state.tasks.find(t=>t.id===state.followTask);
      if(target){state.selected=target.id;state.followTask=null;followHit=true;}
    }
    window.__activity=data.activity||[];
    document.getElementById('dot').className='dot'+(data.error?' bad':'');
    document.getElementById('sync').textContent=data.error?('同步失败：'+data.error):('已同步 '+fmtTime(data.synced_at)+' · 自动刷新');
    const banner=document.getElementById('banner');
    if(data.error){banner.style.display='block';banner.textContent='最近一次同步失败，下面显示的是上一次成功的数据：'+data.error;}
    else{banner.style.display='none';}
    const sig=(data.requests_commit||'')+'|'+(data.results_commit||'')+'|'+state.tasks.length;
    if(sig!==state.lastSig||followHit){
      state.lastSig=sig;
      state.output={};
      state.detail=null;
      renderStats(); renderList(); renderActivity(); renderDetail();
    }
  }catch(e){
    document.getElementById('dot').className='dot bad';
    document.getElementById('sync').textContent='无法连接监控服务';
  }
}
function renderStats(){
  let run=0,ok=0,bad=0;
  for(const t of state.tasks){
    const s=t.summary||{};
    run+=(s.running||0)+(s.delivery||0);
    ok+=s.completed||0;
    bad+=(s.failed||0)+(s.unknown||0);
  }
  document.getElementById('s-tasks').textContent=state.tasks.length;
  document.getElementById('s-run').textContent=run;
  document.getElementById('s-ok').textContent=ok;
  document.getElementById('s-bad').textContent=bad;
}
function taskChip(t){
  const s=t.summary||{};
  if(s.failed)return ['失败','bad'];
  if(s.unknown)return ['未知','warn'];
  if((s.running||0)+(s.delivery||0)>0)return ['执行中','run'];
  if(s.completed && s.completed===s.total)return ['已完成','ok'];
  return ['等待绿区',''];
}
function renderList(){
  const box=document.getElementById('task-list');
  const q=(document.getElementById('filter')?.value||'').trim().toLowerCase();
  const list=state.tasks.filter(t=>!q ||
    t.id.toLowerCase().includes(q) ||
    t.commands.some(c=>(c.description||'').toLowerCase().includes(q)));
  const counter=document.getElementById('task-count');
  if(counter)counter.textContent=list.length===state.tasks.length?String(state.tasks.length):list.length+' / '+state.tasks.length;
  if(!state.tasks.length){box.innerHTML='<div class="empty">暂无任务<br><span class="hint">蓝区下发指令后会出现在这里</span></div>';return;}
  if(!state.selected)state.selected=state.tasks[0].id;
  if(!list.length){box.innerHTML='<div class="empty">没有匹配的任务<br><span class="hint">换个关键词试试</span></div>';return;}
  box.innerHTML=list.map(t=>{
    const [label,cls]=taskChip(t);
    const extra=(t.command_count||0)>1?(' +'+(t.command_count-1)):'';
    return `<div class="task ${t.id===state.selected?'active':''}" data-id="${esc(t.id)}">
      <div class="top"><span class="id">${esc(t.id)}</span><span class="chip ${cls}" style="margin-left:auto">${label}</span></div>
      <div class="desc">${esc(t.first_summary||'（无说明）')}${extra}</div>
      <div class="time">${fmtTime(t.updated_at||t.created_at)}</div>
    </div>`;
  }).join('');
  box.querySelectorAll('.task').forEach(el=>el.onclick=()=>{state.selected=el.dataset.id;renderList();renderDetail();});
}
function renderActivity(){
  const box=document.getElementById('activity');
  const items=(window.__activity||[]).slice(0,20);
  box.innerHTML=items.length?items.map(a=>`<div><code>${esc((a.subject||'').slice(0,60))}</code><br>${esc(a.branch)} · ${fmtTime(a.date)}</div>`).join(''):'<div class="hint">暂无提交</div>';
}
function renderDetail(){
  const box=document.getElementById('detail');
  const summary=state.tasks.find(x=>x.id===state.selected);
  if(!summary){box.innerHTML='<div class="empty"><b>选择左侧任务</b>查看命令、状态和原始输出。</div>';return;}
  const t=state.detail;
  if(!t||t.id!==summary.id){
    box.innerHTML='<div class="empty">正在加载任务详情…</div>';
    loadDetail(summary.id);
    return;
  }
  const [label,cls]=taskChip(t);
  let html=`<h3>${esc(t.id)} <span class="chip ${cls}">${label}</span></h3>
    <div class="meta">类型 ${t.kind==='group'?'聚合任务':'单条指令'} · 更新 ${fmtTime(t.updated_at||t.created_at)} · 共 ${t.commands.length} 条指令<br>
      任务分支 ${esc((t.requests_commit||'—').slice(0,7))} · 结果分支 ${esc((t.results_commit||'—').slice(0,7))}</div>`;
  if(!t.commands.length)html+='<div class="hint">还没有收到该任务的指令内容。</div>';
  const currentOpen=state.openCmds[t.id];
  let index=0;
  for(const c of t.commands){
    const open=((currentOpen===undefined&&index===0)||currentOpen===c.id)?'open':'';
    index+=1;
    html+=`<details class="cmd" data-cmd="${esc(c.id)}" ${open}>
      <summary><span class="t">${esc(c.summary||c.description||cmdPreview(c)||c.id)}</span>
        <span class="chip ${chipClass(c.state)}">${esc(c.state_label||c.state)}</span></summary>
      <div class="body">
        <div class="kv">
          <span>指令 ID</span><span>${esc(c.id)}</span>
          <span>退出码</span><span>${c.exit_code===null||c.exit_code===undefined?'—':esc(c.exit_code)}${c.timed_out?'（超时）':''}</span>
          <span>执行耗时</span><span>${fmtDur(c.duration_seconds)}</span>
          <span>输出大小</span><span>stdout ${bytes(c.stdout_size)} · stderr ${bytes(c.stderr_size)}</span>
          <span>结果说明</span><span>${esc(humanMessage(c.message))}</span>
        </div>
        ${renderContext(c)}
        ${renderCommand(c)}
        ${['completed','failed','unknown','needs_input'].includes(c.state) ? `
        <div class="tabs">
          <button data-stream="stdout" class="${(state.tab[c.id]||'stdout')==='stdout'?'on':''}">stdout</button>
          <button data-stream="stderr" class="${state.tab[c.id]==='stderr'?'on':''}">stderr</button>
          <a href="/output?task=${encodeURIComponent(t.id)}&cmd=${encodeURIComponent(c.id)}&stream=stdout&download=1" style="margin-left:auto"><button>下载 stdout</button></a>
          <a href="/output?task=${encodeURIComponent(t.id)}&cmd=${encodeURIComponent(c.id)}&stream=stderr&download=1"><button>下载 stderr</button></a>
        </div>
        <div class="output" data-out="${esc(c.id)}">${outputHtml(c)}</div>` : `
        <div class="hint" style="margin-top:8px">等待绿区回传结果…</div>`}
      </div>
    </details>`;
  }
  box.innerHTML=html;
  box.querySelectorAll('details.cmd').forEach(el=>{
    el.addEventListener('toggle',()=>{
      if(el.open){
        state.openCmds[state.selected]=el.dataset.cmd;
        if(el.querySelector('.output'))loadOutput(el.dataset.cmd);
      }else if(state.openCmds[state.selected]===el.dataset.cmd){
        state.openCmds[state.selected]=null;
      }
    });
  });
  box.querySelectorAll('details.cmd[open]').forEach(el=>{if(el.querySelector('.output'))loadOutput(el.dataset.cmd);});
  box.querySelectorAll('.tabs button[data-stream]').forEach(btn=>{
    btn.onclick=()=>{
      const cmd=btn.closest('details').dataset.cmd;
      state.tab[cmd]=btn.dataset.stream;
      delete state.output[cmd];
      btn.parentElement.querySelectorAll('button[data-stream]').forEach(b=>b.classList.toggle('on',b===btn));
      loadOutput(cmd,true);
    };
  });
}
async function loadDetail(id){
  if(state.detailLoading===id)return;
  state.detailLoading=id;
  try{
    const res=await fetch('/api/task?task='+encodeURIComponent(id),{cache:'no-store'});
    const data=await res.json();
    if(data.ok&&state.selected===id){state.detail=data.task;renderDetail();}
  }catch(e){}
  finally{if(state.detailLoading===id)state.detailLoading=null;}
}
function renderContext(c){
  const ctx=c.context;
  if(!ctx||(!(ctx.messages||[]).length&&!ctx.note))return '';
  const msgs=ctx.messages||[];
  let inner='';
  if(ctx.note)inner+=`<div class="note"><div class="who">补充说明</div><div>${esc(ctx.note)}</div></div>`;
  inner+=msgs.map(m=>`<div class="note"><div class="who">${m.role==='user'?'用户':'助手'} · ${fmtTime(m.at)}</div><div>${esc(m.text)}</div></div>`).join('');
  return `<details class="ctx" style="border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin-bottom:10px">
    <summary class="hint" style="cursor:pointer">派单上下文（${msgs.length} 条对话${ctx.note?' + 补充说明':''}）</summary>
    ${inner}
  </details>`;
}
function renderCommand(c){
  const e=c.effective||{};
  const fallback=c.command?(c.command.mode==='powershell'?(c.command.script||''):(c.command.argv||[]).join(' ')):'';
  const effective=e.effective||fallback;
  if(!effective&&!c.command)return '<div class="hint">没有命令内容。</div>';
  let body='';
  if(e.target){
    body+=`<div class="hint" style="margin-bottom:6px">目标环境 <b style="color:var(--ink)">${esc(e.target)}</b>`;
    if(e.targets&&e.targets.length)body+=` · 脚本内还会连接 ${esc(e.targets.join('、'))}`;
    body+='</div>';
  }
  const isRemote=(e.type==='ssh'||e.type==='local-script'||e.type==='plink'||e.type==='stateful');
  body+=`<div class="hint" style="margin-bottom:4px">${isRemote?'实际远程命令':'实际执行命令'}</div>
    <pre class="code">${esc(effective||'（空）')}</pre>`;
  if(e.transport&&e.transport!==effective){
    body+=`<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">查看绿区实际执行/传输层命令</summary><pre class="code">${esc(e.transport)}</pre></details>`;
  }
  if(c.timeout_seconds)body+=`<div class="hint" style="margin-top:6px">超时：${esc(c.timeout_seconds)} 秒</div>`;
  return body;
}
function outputHtml(c){
  const entry=state.output[c.id];
  if(!entry)return '<span class="hint">展开后加载输出…</span>';
  if(entry.loading)return '<span class="hint">加载中…</span>';
  if(entry.error)return '<span class="hint" style="color:var(--bad)">'+esc(entry.error)+'</span>';
  if(entry.binary)return `<span class="hint">二进制输出 ${bytes(entry.size)}，请使用下载按钮保存。</span>`;
  if(!entry.text)return '<span class="hint">（空输出）</span>';
  return esc(entry.text)+(entry.truncated?'\n\n… 已截断，完整内容请下载':'');
}
async function loadOutput(cmd,force){
  if(!state.selected)return;
  const existing=state.output[cmd];
  if(existing && !force && !existing.loading)return;
  state.output[cmd]={loading:true};
  updateOutputBox(cmd);
  const stream=(state.tab[cmd]||'stdout');
  try{
    const res=await fetch(`/api/output?task=${encodeURIComponent(state.selected)}&cmd=${encodeURIComponent(cmd)}&stream=${stream}`);
    const data=await res.json();
    state.output[cmd]=data.ok?{text:data.text,binary:data.binary,size:data.size,truncated:data.truncated}:{error:data.error};
  }catch(e){state.output[cmd]={error:'读取输出失败'};}
  updateOutputBox(cmd);
}
function updateOutputBox(cmd){
  const box=document.querySelector(`.output[data-out="${cmd}"]`);
  if(box)box.innerHTML=outputHtml({id:cmd});
}
async function loadNotes(){
  try{
    const res=await fetch('/api/notes',{cache:'no-store'});
    const data=await res.json();
    const box=document.getElementById('notes');
    box.innerHTML=(data.notes||[]).slice().reverse().map(n=>`<div class="note"><div>${esc(n.text)}</div><div class="who">${esc(n.author)} · ${fmtTime(n.created_at)}</div></div>`).join('')||'<div class="hint" style="margin-top:8px">还没有备注</div>';
  }catch(e){}
}
document.getElementById('note-add').onclick=async()=>{
  const text=document.getElementById('note-text').value.trim();
  if(!text)return;
  const author=document.getElementById('note-author').value.trim()||'我';
  await fetch('/api/notes',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({author,text})});
  document.getElementById('note-text').value='';
  loadNotes();
};
document.getElementById('refresh').onclick=async()=>{
  document.getElementById('sync').textContent='正在刷新…';
  await fetch('/api/refresh',{method:'POST'});
  load();
};
let lastCompose=null;
async function loadServers(){
  try{
    const data=await (await fetch('/api/servers',{cache:'no-store'})).json();
    const servers=data.servers||[];
    const sel=document.getElementById('env-select');
    sel.dataset.servers=JSON.stringify(servers);
    sel.innerHTML=servers.length?servers.map((s,i)=>`<option value="${i}">${esc(s.label)}（${esc(s.user)}@${esc(s.host)}）</option>`).join(''):'<option value="">未配置环境（编辑 local/servers.json）</option>';
  }catch(e){}
}
async function loadContextPreview(){
  try{
    const data=await (await fetch('/api/context?limit=6',{cache:'no-store'})).json();
    const box=document.getElementById('ctx-preview');
    const msgs=data.messages||[];
    box.innerHTML=msgs.length?msgs.map(m=>`<div class="note"><div class="who">${m.role==='user'?'用户':'助手'} · ${fmtTime(m.at)}</div><div>${esc(m.text.slice(0,200))}${m.text.length>200?'…':''}</div></div>`).join(''):'<div class="hint">没有读到会话记录</div>';
  }catch(e){}
}
document.getElementById('compose-btn').onclick=async()=>{
  const sel=document.getElementById('env-select');
  const servers=JSON.parse(sel.dataset.servers||'[]');
  const env=servers[Number(sel.value)];
  const command=document.getElementById('compose-text').value;
  const box=document.getElementById('compose-result');
  if(!env){box.innerHTML='<div class="hint" style="color:var(--bad);margin-top:8px">先在 local/servers.json 配置环境</div>';return;}
  const includeContext=document.getElementById('compose-ctx').checked;
  const contextNote=document.getElementById('compose-note').value;
  const res=await fetch('/api/compose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({host:env.host,user:env.user,command,include_context:includeContext,context_note:contextNote,stateful:document.getElementById('compose-stateful').checked})});
  const data=await res.json();
  if(!data.ok){box.innerHTML=`<div class="hint" style="color:var(--bad);margin-top:8px">${esc(data.error)}</div>`;return;}
  lastCompose=data.request;
  document.getElementById('compose-save').disabled=false;
  const e=data.preview||{};
  const ctx=data.request.context||{};
  const ctxCount=(ctx.messages||[]).length;
  box.innerHTML=`<div class="hint" style="margin-top:10px">实际远程命令</div>
    <pre class="code">${esc(e.effective||'')}</pre>
    <div class="hint" style="margin-top:6px">将附带 ${ctxCount} 条对话${ctx.note?' + 1 条补充说明':''}</div>
    ${ctxCount||ctx.note?`<details style="margin-top:6px"><summary class="hint" style="cursor:pointer">查看附带上下文</summary>
      ${ctx.note?`<div class="note"><div class="who">补充说明</div><div>${esc(ctx.note)}</div></div>`:''}
      ${(ctx.messages||[]).map(m=>`<div class="note"><div class="who">${m.role==='user'?'用户':'助手'} · ${fmtTime(m.at)}</div><div>${esc(m.text.slice(0,300))}</div></div>`).join('')}
    </details>`:''}
    <details style="margin-top:6px"><summary class="hint" style="cursor:pointer">传输层命令与请求 JSON</summary>
      <pre class="code">${esc(e.transport||'')}</pre>
      <pre class="code">${esc(JSON.stringify(data.request,null,2))}</pre>
    </details>
    <div style="display:flex;gap:8px;margin-top:6px">
      <button id="compose-copy">复制 JSON</button>
      <button id="compose-send">派单到绿区</button>
    </div>`;
  document.getElementById('compose-copy').onclick=()=>navigator.clipboard.writeText(JSON.stringify(data.request,null,2));
  document.getElementById('compose-send').onclick=async()=>{
    if(!lastCompose)return;
    if(!confirm('这会写入传输仓库，绿区服务可能立即执行。确认派单？'))return;
    const btn=document.getElementById('compose-send');
    btn.disabled=true;btn.textContent='派单中…';
    const res2=await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:lastCompose})});
    const sent=await res2.json();
    if(sent.ok){
      box.innerHTML+=`<div class="hint" style="margin-top:8px;color:var(--ok)">已派单：${esc(sent.id||'')}<br>正在等待绿区执行，任务会自动选中，结果出来后直接显示。</div>`;
      btn.textContent='已派单';
      state.followTask=sent.id||null;
      load();
    }else{
      box.innerHTML+=`<div class="hint" style="margin-top:8px;color:var(--bad)">派单失败：${esc(sent.error)}<br>本地 JSON 已保留，可重试同一条。</div>`;
      btn.disabled=false;btn.textContent='重试派单';
    }
  };
};
document.getElementById('compose-save').onclick=async()=>{
  if(!lastCompose)return;
  const res=await fetch('/api/outbox',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:lastCompose})});
  const data=await res.json();
  const box=document.getElementById('compose-result');
  if(data.ok){
    box.innerHTML+=`<div class="hint" style="margin-top:8px;color:var(--ok)">已保存：${esc(data.path)}<br>对蓝区说“发出去”即可提交到绿区。</div>`;
    document.getElementById('compose-save').disabled=true;
  }else{
    box.innerHTML+=`<div class="hint" style="margin-top:8px;color:var(--bad)">保存失败：${esc(data.error)}</div>`;
  }
};
document.getElementById('filter').addEventListener('input',renderList);
window.addEventListener('load',()=>{load();loadNotes();loadServers();loadContextPreview();setInterval(load,1500);});
</script>
</body>
</html>
"""


CHAT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Green Bridge 对话</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--ink:#1c2330;--muted:#6b7480;--line:#e4e8ee;--accent:#2f6df6;--ok:#1f9d5a;--bad:#d0453c;--warn:#c07a12;--chip:#eef2f8;--user:#2f6df6}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--panel:#1a2029;--ink:#e7ecf3;--muted:#98a2b0;--line:#2a323d;--accent:#6f9bff;--ok:#43c07c;--bad:#ff6f63;--warn:#e0a33e;--chip:#222a35;--user:#3b63b8}}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",sans-serif;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line)}
.brand{font-weight:650;font-size:15px}.brand small{color:var(--muted);font-weight:400;margin-left:6px;font-size:12px}
.status{margin-left:auto;display:flex;align-items:center;gap:10px;font-size:12px;color:var(--muted)}
.status a{color:var(--accent);text-decoration:none}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok)}.dot.bad{background:var(--bad)}
main{flex:1;overflow:auto;padding:22px 0 10px}
.wrap{width:min(900px,92vw);margin:0 auto;display:flex;flex-direction:column;gap:18px}
.msg{display:flex;flex-direction:column;max-width:100%}
.msg.user{align-items:flex-end}
.bubble{background:var(--user);color:#fff;border-radius:16px 16px 4px 16px;padding:9px 14px;max-width:82%;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.bubble .who{font-size:11px;opacity:.8;margin-bottom:2px;display:flex;gap:8px;align-items:center}
.bubble pre{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
.bubble details summary{color:#fff;cursor:pointer;font-size:12px;opacity:.9;margin-top:4px}
.bubble details pre{margin-top:4px;max-height:260px;overflow:auto}
.msg.assistant{align-items:flex-start}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px 14px 14px 4px;padding:11px 13px;max-width:100%;width:100%;box-shadow:0 1px 2px rgba(0,0,0,.04)}
.card .head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.chip{display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;background:var(--chip);color:var(--muted);font-size:11px;white-space:nowrap}
.chip.ok{color:var(--ok)}.chip.bad{color:var(--bad)}.chip.run{color:var(--accent)}.chip.warn{color:var(--warn)}
.target{color:var(--ink);font-weight:550}
pre.out{margin:8px 0 0;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 11px;max-height:300px;overflow:auto;white-space:pre-wrap;word-break:break-word;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
pre.cmdtext{max-height:200px;overflow:auto}
.hint{color:var(--muted);font-size:12px}
.actions{display:flex;gap:8px;margin-top:8px}
button{font:inherit;cursor:pointer;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:4px 11px}
button:hover{border-color:var(--accent);color:var(--accent)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.primary:disabled{opacity:.5;cursor:default}
footer{border-top:1px solid var(--line);background:var(--panel);padding:12px 0 16px}
.composer{width:min(900px,92vw);margin:0 auto}
.toprow{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:5px 9px;max-width:260px}
.inputrow{display:flex;gap:10px;align-items:flex-end}
.inputrow select{max-width:210px;height:46px;flex:0 0 auto}
textarea{flex:1;resize:none;min-height:46px;max-height:160px;background:var(--bg);border:1px solid var(--line);border-radius:12px;color:var(--ink);padding:11px 12px;font:inherit;line-height:1.5}
textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
.empty{color:var(--muted);text-align:center;padding:30px}
</style>
</head>
<body>
<header>
  <div class="brand">Green Bridge<small>对话</small></div>
  <div class="status"><span class="dot" id="dot"></span><span id="status">连接中…</span><a href="/">任务视图</a></div>
</header>
<main><div class="wrap" id="chat"><div class="empty">正在加载…</div></div></main>
<footer>
  <div class="composer">
    <div class="inputrow">
      <select id="env"></select>
      <textarea id="input" rows="1" placeholder="输入要在目标环境执行的命令，例如 npu-smi info（Enter 发送，Shift+Enter 换行）"></textarea>
      <button class="primary" id="send">发送</button>
    </div>
    <div class="toprow" style="margin:6px 0 0">
      <label class="hint" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="stateful" checked> 保持目录（cd 后下一条还在原目录）
      </label>
      <span class="hint" id="envhint">从绿区 SSH 到该环境，密钥留在绿区</span>
    </div>
  </div>
</footer>
<script>
const LABELS=%LABELS%;
const S={tasks:[],items:[],pending:[],queue:[],active:null,servers:[],outputs:{},lastSig:'',sending:false,stick:true,autoLoaded:{}};
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function fmtTime(v){if(!v)return '—';const d=new Date(v);return isNaN(d)?v:d.toLocaleString('zh-CN',{hour12:false});}
function dur(v){return (v===null||v===undefined)?'':' · '+Number(v).toFixed(2)+'s';}
function chipClass(s){if(s==='completed')return 'ok';if(s==='failed')return 'bad';if(s==='running'||s==='accepted'||s==='queued')return 'run';if(s==='unknown'||s==='delivery_pending')return 'warn';return '';}
function label(s){return LABELS[s]||s||'未知';}
function humanMessage(m){const map={'Local command exited; output is captured without decoding.':'命令已结束','Command timed out; captured output is partial and child processes may still be running.':'命令超时，输出不完整','Command could not be started.':'命令无法启动'};return map[m]||m||'';}
function commandText(c){return (c&&c.effective&&c.effective.effective)||'';}
function target(c){return (c&&c.effective&&c.effective.target)||'';}
function keyOf(taskId,cmdId){return taskId+'/'+cmdId;}

async function loadServers(){
  try{
    const d=await (await fetch('/api/servers',{cache:'no-store'})).json();
    S.servers=d.servers||[];
    const sel=document.getElementById('env');
    sel.innerHTML=S.servers.map((s,i)=>`<option value="${i}">${esc(s.label)}${s.user&&s.user!=='root'?' · '+esc(s.user):''}</option>`).join('')||'<option value="">未配置环境</option>';
    const saved=localStorage.getItem('gb-env');
    if(saved!==null&&S.servers[Number(saved)])sel.value=saved;
    sel.onchange=()=>localStorage.setItem('gb-env',sel.value);
  }catch(e){}
}
function flatten(){
  const items=[];
  for(const t of S.tasks){
    for(const c of (t.commands||[])){
      items.push({task:t,cmd:c,stamp:c.updated_at||t.updated_at||t.created_at||''});
    }
  }
  items.sort((a,b)=>a.stamp<b.stamp?-1:1);
  return items.slice(-40);
}
async function loadRecent(){
  try{
    const d=await (await fetch('/api/recent?limit=30',{cache:'no-store'})).json();
    S.tasks=d.tasks||[];
    S.items=flatten();
    render();
    autoLoadOutputs();
    checkActive();
  }catch(e){}
}
async function loadVersion(){
  try{
    const v=await (await fetch('/api/version',{cache:'no-store'})).json();
    document.getElementById('dot').className='dot'+(v.error?' bad':'');
    document.getElementById('status').textContent=v.error?('同步失败：'+v.error):('已同步 '+fmtTime(v.synced_at));
    const sig=(v.requests_commit||'')+'|'+(v.results_commit||'')+'|'+v.task_count;
    if(sig!==S.lastSig){S.lastSig=sig;await loadRecent();}
  }catch(e){
    document.getElementById('dot').className='dot bad';
    document.getElementById('status').textContent='无法连接监控服务';
  }
}
function render(){
  const box=document.getElementById('chat');
  const nearBottom=S.stick;
  const known=new Set(S.tasks.map(t=>t.id));
  S.pending=S.pending.filter(p=>!(p.taskId&&known.has(p.taskId)));
  let html='';
  for(const item of S.items){
    const c=item.cmd;
    const text=commandText(c)||'(无命令)';
    const lines=text.split('\n').filter(x=>x.trim());
    const preview=lines.slice(0,3).join('\n').slice(0,400);
    const more=lines.length>3?`<details><summary>展开完整命令（${lines.length} 行）</summary><pre>${esc(text)}</pre></details>`:'';
    html+=`<div class="msg user"><div class="bubble"><div class="who"><span>${esc(target(c)||'—')}</span><span>${fmtTime(c.updated_at||item.task.updated_at)}</span></div><pre class="cmdtext">${esc(preview)}${lines.length>3?'\n…':''}</pre>${more}</div></div>`;
    html+=assistantCard(item.task,c);
  }
  for(const p of S.pending){
    html+=`<div class="msg user"><div class="bubble"><div class="who"><span>${esc(p.env.user)}@${esc(p.env.host)}</span><span>刚刚</span></div><pre>${esc(p.text)}</pre></div></div>`;
    html+=`<div class="msg assistant"><div class="card"><div class="head"><span class="chip ${p.state==='error'?'bad':'run'}">${p.state==='sending'?'发送中':p.state==='queued'?'等待绿区执行':'失败'}</span><span class="target">${esc(p.env.user)}@${esc(p.env.host)}</span></div>${p.error?`<pre class="out">${esc(p.error)}</pre>`:'<div class="hint" style="margin-top:8px">已提交，等待绿区回传结果…</div>'}</div></div>`;
  }
  let ahead=S.pending.length;
  for(const q of S.queue){
    html+=`<div class="msg user"><div class="bubble"><div class="who"><span>${esc(q.env.user)}@${esc(q.env.host)}</span><span>排队</span></div><pre>${esc(q.text)}</pre></div></div>`;
    html+=`<div class="msg assistant"><div class="card"><div class="head"><span class="chip run">排队中</span><span class="target">${esc(q.env.user)}@${esc(q.env.host)}</span><span style="margin-left:auto">前面还有 ${ahead} 条</span></div><div class="hint" style="margin-top:8px">等前面的命令完成后自动发送，保证 cd 顺序。</div></div></div>`;
    ahead++;
  }
  if(!html)html='<div class="empty">还没有命令。在下面输入一条，发送到选定的环境。</div>';
  box.innerHTML=html;
  if(nearBottom){
    const mainEl=document.querySelector('main');
    requestAnimationFrame(()=>{mainEl.scrollTop=mainEl.scrollHeight;});
    setTimeout(()=>{mainEl.scrollTop=mainEl.scrollHeight;},250);
  }
}
function assistantCard(task,c){
  const k=keyOf(task.id,c.id);
  const out=S.outputs[k];
  const terminal=['completed','failed','unknown','needs_input'].includes(c.state);
  let body='';
  if(!terminal)body='<div class="hint" style="margin-top:8px">等待绿区回传结果…</div>';
  else if(out&&out.loading)body='<div class="hint" style="margin-top:8px">加载输出…</div>';
  else if(out&&out.error)body=`<div class="hint" style="margin-top:8px;color:var(--bad)">${esc(out.error)}</div>`;
  else if(out&&out.binary)body='<div class="hint" style="margin-top:8px">二进制输出，请下载</div>';
  else if(out&&out.text!==undefined)body=`<pre class="out">${esc(out.text)}${out.truncated?'\n… 已截断':''}</pre>`;
  else body='<div class="hint" style="margin-top:8px">点击“查看输出”加载</div>';
  const actions=terminal?`<div class="actions">
      <button data-out-btn="${esc(k)}" data-task="${esc(task.id)}" data-cmd="${esc(c.id)}">查看输出</button>
      <a href="/output?task=${encodeURIComponent(task.id)}&cmd=${encodeURIComponent(c.id)}&stream=stdout&download=1"><button>下载 stdout</button></a>
      <a href="/output?task=${encodeURIComponent(task.id)}&cmd=${encodeURIComponent(c.id)}&stream=stderr&download=1"><button>下载 stderr</button></a>
    </div>`:'';
  return `<div class="msg assistant"><div class="card" data-task="${esc(task.id)}" data-cmd="${esc(c.id)}">
    <div class="head">
      <span class="chip ${chipClass(c.state)}">${esc(label(c.state))}</span>
      ${c.shell_state?'<span class="chip">保持目录</span>':''}
      <span class="target">${esc(target(c)||'—')}</span>
      <span>${c.exit_code===null||c.exit_code===undefined?'':'退出码 '+esc(c.exit_code)}${c.timed_out?'（超时）':''}</span>
      <span>${esc(dur(c.duration_seconds))}</span>
      <span style="margin-left:auto">${fmtTime(c.updated_at||task.updated_at)}</span>
    </div>
    ${c.message?`<div class="hint" style="margin-top:4px">${esc(humanMessage(c.message))}</div>`:''}
    <div class="outbox" data-outbox="${esc(k)}">${body}</div>
    ${actions}
  </div></div>`;
}
async function loadOutput(taskId,cmdId,force){
  const k=keyOf(taskId,cmdId);
  if(S.outputs[k]&&!force)return;
  S.outputs[k]={loading:true};
  updateCard(k);
  try{
    const d=await (await fetch(`/api/output?task=${encodeURIComponent(taskId)}&cmd=${encodeURIComponent(cmdId)}&stream=stdout`)).json();
    S.outputs[k]=d.ok?{text:(d.text||'').slice(0,20000),truncated:d.truncated||(d.text||'').length>20000,binary:d.binary,size:d.size}:{error:d.error};
  }catch(e){S.outputs[k]={error:'读取输出失败'};}
  updateCard(k);
}
function updateCard(k){
  const btn=document.querySelector(`[data-out-btn="${CSS.escape(k)}"]`);
  if(!btn)return;
  const card=btn.closest('.card');
  const slot=card.querySelector('[data-outbox]');
  const out=S.outputs[k];
  if(!slot)return;
  if(!out||out.loading){slot.innerHTML='<div class="hint" style="margin-top:8px">加载输出…</div>';return;}
  let node=null;
  if(out.error){node=document.createElement('div');node.className='hint';node.style.color='var(--bad)';node.textContent=out.error;}
  else if(out.binary){node=document.createElement('div');node.className='hint';node.textContent='二进制输出，请下载';}
  else{node=document.createElement('pre');node.className='out';node.textContent=(out.text||'(空输出)')+(out.truncated?'\n… 已截断':'');}
  slot.innerHTML='';
  slot.appendChild(node);
  btn.textContent=out.text!==undefined?'刷新输出':'查看输出';
}
function autoLoadOutputs(){
  const done=S.items.filter(i=>['completed','failed','unknown'].includes(i.cmd.state)).slice(-3);
  for(const item of done){
    const k=keyOf(item.task.id,item.cmd.id);
    if(!S.outputs[k]&&!S.autoLoaded[k]){S.autoLoaded[k]=true;loadOutput(item.task.id,item.cmd.id);}
  }
  setTimeout(()=>{if(S.stick){const m=document.querySelector('main');m.scrollTop=m.scrollHeight;}},400);
}
function chatContext(){
  const msgs=[];
  for(const item of S.items.slice(-4)){
    msgs.push({role:'user',text:commandText(item.cmd).slice(0,400),at:item.cmd.updated_at||item.task.updated_at});
    msgs.push({role:'assistant',text:`${label(item.cmd.state)}${item.cmd.exit_code!==undefined&&item.cmd.exit_code!==null?'，退出码 '+item.cmd.exit_code:''}`,at:item.cmd.updated_at||item.task.updated_at});
  }
  return msgs;
}
async function send(){
  const input=document.getElementById('input');
  const text=input.value.trim();
  if(!text)return;
  const sel=document.getElementById('env');
  const env=S.servers[Number(sel.value)];
  if(!env){alert('先配置 local/servers.json 里的环境');return;}
  S.queue.push({qid:'q'+Date.now()+Math.random().toString(16).slice(2),env,text,
    stateful:document.getElementById('stateful').checked});
  input.value='';S.stick=true;render();pump();
}
function taskStates(id){
  const t=S.tasks.find(x=>x.id===id);
  if(!t||!t.commands||!t.commands.length)return null;
  return t.commands.map(c=>c.state);
}
function checkActive(){
  if(!S.active)return;
  const states=taskStates(S.active.taskId);
  const terminal=['completed','failed','unknown','needs_input'];
  if(states&&states.every(s=>terminal.includes(s))){
    S.active=null;render();pump();
  }else if(Date.now()-S.active.since>300000){
    const p=S.pending.find(x=>x.taskId===S.active.taskId);
    if(p){p.state='error';p.error='等待绿区回传超时';}
    S.active=null;render();pump();
  }
}
async function pump(){
  if(S.active||S.sending||!S.queue.length)return;
  const item=S.queue.shift();
  S.sending=true;
  const pending={id:item.qid,env:item.env,text:item.text,state:'sending'};
  S.pending.push(pending);S.stick=true;render();
  try{
    const composed=await (await fetch('/api/compose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({host:item.env.host,user:item.env.user,command:item.text,include_context:false,messages:chatContext(),stateful:item.stateful})})).json();
    if(!composed.ok)throw new Error(composed.error||'生成失败');
    const sent=await (await fetch('/api/dispatch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:composed.request})})).json();
    if(!sent.ok)throw new Error(sent.error||'派单失败');
    pending.state='queued';pending.taskId=sent.id;
    S.active={taskId:sent.id,since:Date.now()};
    render();
    await loadVersion();
  }catch(e){
    pending.state='error';pending.error=e.message||'发送失败';render();
  }finally{
    S.sending=false;
    if(!S.active)setTimeout(pump,0);
  }
}
function autoGrow(el){el.style.height='auto';el.style.height=Math.min(160,el.scrollHeight)+'px';}
document.getElementById('input').addEventListener('input',e=>autoGrow(e.target));
document.getElementById('input').addEventListener('keydown',e=>{
  if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();send();}
});
document.getElementById('send').onclick=send;
document.querySelector('main').addEventListener('scroll',()=>{
  const el=document.querySelector('main');
  S.stick=(el.scrollHeight-el.scrollTop-el.clientHeight)<120;
});
document.addEventListener('click',e=>{
  const btn=e.target.closest('[data-out-btn]');
  if(btn)loadOutput(btn.dataset.task,btn.dataset.cmd,true);
});
loadServers().then(loadVersion);
setInterval(loadVersion,1500);
setInterval(checkActive,3000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    monitor: Monitor | None = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, value, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        monitor = self.monitor
        if parsed.path == "/chat":
            labels = json.dumps(STATE_LABELS, ensure_ascii=False)
            html = CHAT_HTML.replace("%LABELS%", labels)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/":
            labels = json.dumps(STATE_LABELS, ensure_ascii=False)
            html = INDEX_HTML.replace("%LABELS%", labels)
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/state":
            payload = monitor.compact_state()
            self._json(payload)
            return
        if parsed.path == "/api/version":
            self._json({"ok": True, **monitor.version()})
            return
        if parsed.path == "/api/task":
            params = parse_qs(parsed.query)
            task_id = (params.get("task") or [""])[0]
            detail = monitor.task_detail(task_id)
            if detail is None:
                self._json({"ok": False, "error": "not found"}, status=404)
            else:
                self._json({"ok": True, "task": detail})
            return
        if parsed.path == "/api/recent":
            params = parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["30"])[0])
            except ValueError:
                limit = 30
            limit = max(1, min(50, limit))
            self._json({"ok": True, "tasks": monitor.recent_tasks(limit),
                        "results_commit": monitor.head(monitor.results_branch)})
            return
        if parsed.path == "/api/output":
            params = parse_qs(parsed.query)
            task = (params.get("task") or [""])[0]
            cmd = (params.get("cmd") or [""])[0]
            stream = (params.get("stream") or ["stdout"])[0]
            try:
                data, _ = monitor.output_bytes(task, cmd, stream)
                try:
                    text = data.decode("utf-8")
                    binary = False
                except UnicodeDecodeError:
                    text = ""
                    binary = True
                truncated = len(text.encode("utf-8")) > MAX_TEXT_BYTES
                if truncated:
                    text = text.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
                self._json({
                    "ok": True, "task": task, "cmd": cmd, "stream": stream,
                    "size": len(data), "binary": binary, "text": text,
                    "truncated": truncated,
                })
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=404)
            return
        if parsed.path == "/output":
            params = parse_qs(parsed.query)
            task = (params.get("task") or [""])[0]
            cmd = (params.get("cmd") or [""])[0]
            stream = (params.get("stream") or ["stdout"])[0]
            download = (params.get("download") or [""])[0] == "1"
            try:
                data, _ = monitor.output_bytes(task, cmd, stream)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if download:
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{task}-{cmd}-{stream}.log"',
                )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/servers":
            self._json({"servers": monitor.servers()})
            return
        if parsed.path == "/api/context":
            params = parse_qs(parsed.query)
            try:
                limit = int((params.get("limit") or ["6"])[0])
            except ValueError:
                limit = 6
            limit = max(1, min(20, limit))
            self._json({"ok": True, **session_context(limit=limit)})
            return
        if parsed.path == "/api/notes":
            self._json({"notes": monitor.notes()})
            return
        if parsed.path == "/healthz":
            self._json({"ok": True})
            return
        self._json({"ok": False, "error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        monitor = self.monitor
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(length, 512 * 1024)) if length else b""
        if parsed.path == "/api/notes":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                notes = monitor.add_note(
                    str(payload.get("author") or "本机"), str(payload.get("text") or ""))
                self._json({"ok": True, "notes": notes})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/compose":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                self._json({"ok": True, **monitor.compose(payload)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/outbox":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                path = monitor.save_outbox(payload.get("request"))
                self._json({"ok": True, "path": str(path)})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/dispatch":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
                self._json({"ok": True, **monitor.dispatch(payload.get("request"))})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=400)
            return
        if parsed.path == "/api/refresh":
            threading.Thread(target=monitor.refresh, kwargs={"force": True}, daemon=True).start()
            self._json({"ok": True})
            return
        self._json({"ok": False, "error": "not found"}, status=404)


def main() -> int:
    parser = argparse.ArgumentParser(description="蓝绿协作监控台（只读）")
    parser.add_argument("--config", default=str(SKILL_DIR / "local" / "blue.json"))
    parser.add_argument("--monitor-git", default=None)
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--interval", type=int, default=2)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.exists():
        print(f"找不到蓝区配置：{config_path}")
        return 1
    config = read_json(config_path, {}) or {}
    git_dir = Path(config.get("git_dir", ""))
    if not git_dir.exists():
        print(f"找不到蓝区 bare clone：{git_dir}")
        return 1
    monitor_git = Path(args.monitor_git).expanduser() if args.monitor_git else git_dir.parent / "monitor-control.git"

    monitor = Monitor(config_path, monitor_git, max(2, args.interval))
    monitor.refresh(force=True)
    threading.Thread(target=monitor.run, daemon=True).start()

    Handler.monitor = monitor
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"蓝绿协作监控台：{url}")
    print(f"监控克隆：{monitor_git}")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
