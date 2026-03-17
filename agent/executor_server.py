"""
executor_server.py — Minimal HTTP server that exposes execute_block() over localhost.

Endpoint:
  POST /exec
  Body: JSON instruction block, e.g.
    {"command":"file_op","action":"read","path":"README.md"}
  Response: JSON result from execute_block()

Start:
  python agent/executor_server.py [--port 4001]

Used by api-server.js (cursor bridge) to run local file/shell operations
without spawning a new Python process for each tool call.
"""

import argparse
import json
import sys
import os
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

# Ensure agent/ is on the path so executor/file_ops can be imported
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from executor import execute_block  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/exec":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                block = json.loads(raw)
            except Exception as e:
                self._send_json({"error": f"Invalid JSON: {e}"}, 400)
                return

            try:
                result = execute_block(block)
            except Exception as e:
                result = {"command": block.get("command", ""), "success": False,
                          "stdout": "", "stderr": str(e), "returncode": -1}

            self._send_json(result)
            return

        if self.path == "/file-chat":
            # Forward to api-server.js /v1/file-chat
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except Exception as e:
                self._send_json({"ok": False, "error": f"Invalid JSON: {e}"}, 400)
                return

            api_url = "http://127.0.0.1:3000/v1/file-chat"
            try:
                req = urllib.request.Request(
                    api_url,
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=660) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                self._send_json(result)
            except urllib.error.URLError as e:
                self._send_json({"ok": False, "error": f"api-server unreachable: {e.reason}"}, 502)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True})
        elif self.path == "/file-chat":
            self._send_json({"ok": True, "hint": "POST /file-chat with {file_path, message, agentId}"})
        else:
            self._send_json({"error": "Not found"}, 404)


def main():
    parser = argparse.ArgumentParser(description="AgentPilot executor HTTP server")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), _Handler)
    print(f"[executor-server] listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[executor-server] stopped")


if __name__ == "__main__":
    main()
