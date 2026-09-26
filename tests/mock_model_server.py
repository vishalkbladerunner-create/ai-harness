#!/usr/bin/env python3
"""
OpenAI-compatible mock model server for API-free end-to-end tests.

Why it exists: Phase 0.4 and Phase 6.1 require proving the whole pipeline
(entrypoint -> litellm -> agent loop -> guarded environment -> report) without
credentials, and CI/evaluation may have no endpoint at all. The mock speaks just
enough of the OpenAI chat-completions protocol to drive upstream's LitellmModel
unchanged, so the test exercises the real parsing code path.

Hackathon criteria served: Phase 6.1 (dry-run mode / no API calls), Phase 0.4
(prove end-to-end before building further).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "guarded-mini-mock/1.0"

    def log_message(self, format, *args):  # keep test output clean
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            payload = {}
        record = {
            "path": self.path,
            "model": payload.get("model"),
            "temperature": payload.get("temperature"),
            "seed": payload.get("seed"),
            "n_messages": len(payload.get("messages") or []),
            "tools": bool(payload.get("tools")),
        }
        self.server.requests.append(record)  # type: ignore[attr-defined]
        outputs = self.server.outputs  # type: ignore[attr-defined]
        index = min(self.server.calls, len(outputs) - 1)  # type: ignore[attr-defined]
        self.server.calls += 1  # type: ignore[attr-defined]
        output = outputs[index]

        tool_calls = output.get("tool_calls") or []
        message = {"role": "assistant", "content": output.get("content") or ""}
        if tool_calls:
            message["tool_calls"] = tool_calls
        body = {
            "id": f"chatcmpl-mock-{self.server.calls}",  # type: ignore[attr-defined]
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.get("model") or "mock",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": {"prompt_tokens": 64, "completion_tokens": 32, "total_tokens": 96},
        }
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            data = json.dumps({"object": "list", "data": [{"id": "mock-model", "object": "model"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()


class MockModelServer:
    """Scripted OpenAI-compatible endpoint on a loopback port."""

    def __init__(self, outputs: list[dict]):
        self.outputs = outputs
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.outputs = outputs  # type: ignore[attr-defined]
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.calls = 0  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def requests(self) -> list[dict]:
        return self.httpd.requests  # type: ignore[attr-defined]

    def start(self) -> "MockModelServer":
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=5)


def main() -> int:
    """CLI: serve a canned scenario so a human can point a live harness at it."""
    import argparse
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from harness.dryrun import default_scenario, fixture_scenario

    parser = argparse.ArgumentParser(description="scripted OpenAI-compatible mock model server")
    parser.add_argument("--scenario", choices=["default", "fixture"], default="default")
    parser.add_argument("--port", type=int, default=11435)
    args = parser.parse_args()
    outputs = fixture_scenario() if args.scenario == "fixture" else default_scenario()
    server = MockModelServer(outputs)
    server.httpd.server_close()
    server.httpd = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    server.httpd.outputs = outputs  # type: ignore[attr-defined]
    server.httpd.requests = []  # type: ignore[attr-defined]
    server.httpd.calls = 0  # type: ignore[attr-defined]
    print(f"mock model server on {server.base_url} (scenario={args.scenario})")
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
