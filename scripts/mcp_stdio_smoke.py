"""MCP stdio acceptance smoke for the Fufu Archive server.

This is not a unit test: it spawns the real server as a child process and
speaks newline-delimited JSON-RPC 2.0 over its stdio pipes, exactly as an MCP
client does. It exists because the in-repo pytest suite can only exercise the
MCP tools where the optional ``mcp`` SDK is importable, and the SDK is absent
from the project virtualenv.

The smoke never touches the configured production archive: it initializes a
throwaway database in a temporary directory and points the child at that path.

Usage::

    python scripts/mcp_stdio_smoke.py --server-python <python-with-mcp>

Exit code 0 means the handshake completed and every expected tool was listed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "kb_search",
    "kb_add",
    "kb_list",
    "kb_update",
    "kb_audit_events",
    "kb_candidate_add",
    "kb_candidate_approve",
    "kb_candidate_reject",
    "kb_candidate_commit",
    "kb_candidate_list",
}


def _send(process: subprocess.Popen, payload: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()


def _handshake(process: subprocess.Popen) -> dict[int, dict]:
    """Read server messages until every request id has a response."""
    responses: dict[int, dict] = {}
    assert process.stdout is not None

    def reader() -> None:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                responses[int(message["id"])] = message

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    _send(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "fufu-archive-smoke", "version": "1.0"},
            },
        },
    )
    _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    deadline = time.time() + 60
    while time.time() < deadline and {1, 2} - set(responses):
        time.sleep(0.05)
    return responses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-python",
        default=sys.executable,
        help="interpreter that can import the optional 'mcp' SDK",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(REPO_ROOT / "src"))
    from fufu_archive.db import initialize_database

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "smoke.sqlite3"
        initialize_database(db_path)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
        }
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        env["FUFU_ARCHIVE_DB_PATH"] = str(db_path)
        process = subprocess.Popen(
            [args.server_python, "-m", "fufu_archive.mcp_server"],
            cwd=str(REPO_ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            responses = _handshake(process)
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()

    stderr = process.stderr.read() if process.stderr is not None else ""
    init = responses.get(1)
    listing = responses.get(2)
    if init is None or listing is None or "error" in (init or {}):
        print(json.dumps(
            {"ok": False, "initialize": init, "tools": listing, "stderr": stderr[-2000:]},
            ensure_ascii=False,
        ))
        return 1

    names = {
        tool["name"] for tool in listing.get("result", {}).get("tools", [])
    }
    missing = sorted(EXPECTED_TOOLS - names)
    print(json.dumps(
        {
            "ok": not missing,
            "server": init.get("result", {}).get("serverInfo", {}),
            "protocolVersion": init.get("result", {}).get("protocolVersion"),
            "tool_count": len(names),
            "missing_tools": missing,
            "stderr_tail": stderr[-400:],
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
