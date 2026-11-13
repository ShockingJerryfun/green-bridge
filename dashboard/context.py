#!/usr/bin/env python3
"""Print a context object for a green-bridge request without calling a model.

Reads the most recently active Kimi Code session log and extracts the last
user/assistant messages. Attach the output to a request's `context` field.

Usage:
    python3 context.py --limit 6 --note "这次只读" > context.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard import session_context  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成本地会话上下文 JSON")
    parser.add_argument("--limit", type=int, default=6)
    parser.add_argument("--note", default="")
    parser.add_argument("--no-messages", action="store_true")
    args = parser.parse_args()

    context = session_context(limit=max(1, min(20, args.limit)))
    context["source"] = "agent"
    context["note"] = args.note[:2000]
    if args.no_messages:
        context["messages"] = []
    print(json.dumps(context, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
