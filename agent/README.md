# Windows AI Agent Executor

Uses AgentPilot web bridge to drive ChatGPT; no API key. AI plans tasks and outputs JSON instructions; local executor runs them on Windows.

## Layout

```
agent/
├── json_parser.py    # JSON instruction parsing (multi-strategy fallback)
├── executor.py       # Local execution core
├── agent_loop.py     # AI loop (calls web bridge)
├── system_prompt.txt # Editable system prompt
├── requirements.txt
└── README.md
```

## Flow

```
User input
    ↓
agent_loop.py → AgentPilot web bridge (ChatGPT Web CDP)
    ↓ AI response with JSON blocks
executor.py → extract JSON → run locally (PowerShell/cmd/Python)
    ↓ result
agent_loop.py → send back to AI → AI continues
    ↓
Until AI outputs "✅ Task complete" or no more JSON
```

## Prerequisites

1. **Chrome and log in to ChatGPT** (required):
   ```powershell
   npm run chrome
   ```
   Open https://chatgpt.com/ in the browser and log in.
2. **Bridge API**: started automatically by `npm run agent`, or run `npm run api` manually.

## Run

```powershell
# Recommended: one-shot (starts bridge API if needed)
npm run agent
npm run agent "find 10 US-Iran news and put on desktop"

# Start a new conversation (fresh ChatGPT thread)
npm run agent "/new your task"

# Or run Python directly (run npm run api first)
npm run agent:raw
python agent/agent_loop.py "task description"

# Test executor only (no AI; paste AI response manually)
python agent/executor.py
```

Use **`/new`** before your message to open a new chat (e.g. `/new find 10 news` or `"/new your task"` in one-shot).

## Config

- `AGENTPILOT_URL` env: default `http://127.0.0.1:3000/chat`
- Edit `SYSTEM_PROMPT` in `agent_loop.py` to restrict or customize AI behavior.

## JSON instruction format

```json
{
  "command": "powershell",
  "arguments": [
    "$desktop = [Environment]::GetFolderPath('Desktop')",
    "Write-Output 'Done'"
  ]
}
```

Supported commands: `powershell`, `cmd`, `python`, `file_op` (see system_prompt.txt).

## Security

- This program runs AI-generated code locally; use only for trusted tasks.
- Restrict allowed operations in SYSTEM_PROMPT when possible.
