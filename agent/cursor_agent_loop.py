"""
cursor_agent_loop.py — Cursor-specific agent loop

Exposes a small HTTP server (default :3001) that api-server.js can call.
Accepts the full OpenAI messages array (including tool results from Cursor),
builds a rich context prompt, sends it to ChatGPT via the bridge, and returns
the answer in OpenAI chat-completion format.

Endpoint: POST /cursor-chat
  Body:  { messages: [...], model: str, stream: bool }
  Reply: OpenAI chat.completion JSON

This lets api-server.js delegate the entire Cursor tool-round to Python,
keeping JS thin and Python responsible for prompt engineering.
"""

import os
import sys
import json
import time
import math
import socket
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

# ── Bridge config ────────────────────────────────────────────────────────────

BRIDGE_URL     = os.environ.get("AGENTPILOT_URL",  "http://127.0.0.1:3000/chat")
AGENT_PORT     = int(os.environ.get("CURSOR_AGENT_PORT", "3001"))
MAX_CONTEXT_CHARS = 12_000   # truncate huge file dumps before sending to ChatGPT


# ── Message flattening ───────────────────────────────────────────────────────

def _get_text(item: dict) -> str:
    """Extract plain text from an OpenAI message (handles array content)."""
    c = item.get("content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if p.get("type") in ("text", "input_text") and p.get("text"):
                parts.append(p["text"])
        return "\n".join(parts)
    return ""


def flatten_messages(messages: list) -> str:
    """
    Convert a full Cursor message array (system / user / assistant / tool)
    into a single coherent prompt string for ChatGPT.
    """
    sections = []

    for m in messages:
        role = m.get("role", "user")

        if role == "system":
            text = _get_text(m).strip()
            if text:
                # Keep system prompt concise — drop Cursor's boilerplate tool descriptions
                text = _strip_tool_schema_boilerplate(text)
                if text:
                    sections.append(f"[System]\n{text}")

        elif role == "user":
            text = _get_text(m).strip()
            if text:
                sections.append(f"[User]\n{text}")

        elif role == "assistant":
            text = _get_text(m).strip()
            if text:
                sections.append(f"[Assistant]\n{text}")
            # assistant may carry tool_calls (round 1 response we sent)
            for tc in (m.get("tool_calls") or []):
                fn   = tc.get("function", {})
                name = fn.get("name", tc.get("id", "tool"))
                args = fn.get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                sections.append(f"[Called tool: {name}]\n{args}")

        elif role == "tool":
            content = _get_text(m) or m.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            name = m.get("name") or m.get("tool_call_id") or "tool"
            content = content.strip()
            # Truncate very large tool outputs (e.g. full file dumps)
            if len(content) > MAX_CONTEXT_CHARS:
                content = content[:MAX_CONTEXT_CHARS] + f"\n... [truncated, {len(content)} chars total]"
            if content:
                sections.append(f"[Tool result: {name}]\n{content}")

    return "\n\n".join(sections)


def _strip_tool_schema_boilerplate(text: str) -> str:
    """
    Cursor injects large tool-schema XML/JSON blocks into the system message.
    Strip them so we don't waste ChatGPT context on schema definitions.
    """
    import re
    # Remove <tools>...</tools> or similar XML blocks
    text = re.sub(r"<tools>[\s\S]*?</tools>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<tool_descriptions>[\s\S]*?</tool_descriptions>", "", text, flags=re.IGNORECASE)
    # Remove JSON tool-schema array blocks
    text = re.sub(r"\[\s*\{\s*\"type\"\s*:\s*\"function\"[\s\S]*?\}\s*\]", "", text)
    return text.strip()


# ── Bridge communication ─────────────────────────────────────────────────────

def _chat_via_bridge(prompt: str, new_chat: bool = False) -> str:
    """Send prompt to AgentPilot bridge and return the reply text."""
    body = json.dumps({
        "message": prompt,
        "newChat":  new_chat,
        "agentId":  "cursor",
    }).encode("utf-8")
    req = urllib.request.Request(
        BRIDGE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8") if e.fp else str(e)
        raise RuntimeError(f"Bridge HTTP {e.code}: {err}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Bridge unreachable ({e.reason}). Run: npm run api")

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Bridge returned error"))

    return data.get("result", "")


# ── OpenAI response builder ──────────────────────────────────────────────────

def _make_completion(content: str, model: str, prompt_text: str) -> dict:
    return {
        "id":      "chatcmpl-" + str(int(time.time() * 1000)),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   model,
        "choices": [{
            "index":   0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     math.ceil(len(prompt_text) / 4),
            "completion_tokens": math.ceil(len(content) / 4),
            "total_tokens":      math.ceil((len(prompt_text) + len(content)) / 4),
        },
    }


def _make_error(message: str, status: int = 500) -> tuple:
    return {"error": {"message": message}}, status


# ── HTTP handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access log
        pass

    def do_OPTIONS(self):
        self._cors()
        self.send_response(204)
        self.end_headers()

    def do_POST(self):
        self._cors()
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"error": {"message": "Invalid JSON"}}, 400)
            return

        if self.path.rstrip("/") in ("/cursor-chat", "/v1/cursor-chat"):
            self._handle_cursor_chat(body)
        else:
            self._json({"error": {"message": "Not found"}}, 404)

    def _handle_cursor_chat(self, body: dict):
        messages = body.get("messages", [])
        model    = body.get("model", "gpt-4o")
        stream   = body.get("stream", False)

        if not messages:
            self._json({"error": {"message": "messages required"}}, 400)
            return

        prompt = flatten_messages(messages)
        if not prompt.strip():
            self._json({"error": {"message": "Empty context after flattening"}}, 400)
            return

        print(f"[cursor-agent] flattened {len(messages)} msg(s) → {len(prompt)} chars")

        try:
            reply = _chat_via_bridge(prompt)
        except RuntimeError as e:
            self._json({"error": {"message": str(e)}}, 502)
            return

        if not reply:
            self._json({"error": {"message": "Empty reply from ChatGPT"}}, 502)
            return

        data = _make_completion(reply, model, prompt)

        if stream:
            self._stream(data, reply, model)
        else:
            self._json(data, 200)

    def _stream(self, data: dict, content: str, model: str):
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        chunk_id = data["id"]
        created  = data["created"]
        for i in range(0, len(content), 50):
            chunk = {
                "id": chunk_id, "object": "chat.completion.chunk",
                "created": created, "model": model,
                "choices": [{"index": 0, "delta": {"content": content[i:i+50]}, "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        done = {
            "id": chunk_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        self.wfile.write(f"data: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")


# ── Entry point ──────────────────────────────────────────────────────────────

def main():
    server = HTTPServer(("127.0.0.1", AGENT_PORT), Handler)
    print(f"[cursor-agent] Listening on http://127.0.0.1:{AGENT_PORT}")
    print(f"[cursor-agent] Bridge: {BRIDGE_URL}")
    print(f"[cursor-agent] Endpoint: POST /cursor-chat")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[cursor-agent] Stopped.")


if __name__ == "__main__":
    main()
