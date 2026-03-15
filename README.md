# AgentPilot

Drive local chat and task execution via ChatGPT web — **no API key, no token limits, unlimited use.**

**AgentPilot** — A lightweight AI agent powered by ChatGPT web. No API, no token limits, unlimited use. It can write code, find files, and much more; explore what you can do with it.

---

## Requirements

- **Node.js 18+** · [Download](https://nodejs.org/) or install via CLI:
  ```powershell
  winget install OpenJS.NodeJS.LTS --source winget
  # If terminal cannot find npm, run first:
  $env:Path = "C:\Program Files\nodejs;" + $env:Path
  ```
- **Chrome** browser
- **Windows** (PowerShell)

## Install and first run

```bash
git clone https://github.com/springcell/AgentPilot.git
cd AgentPilot
npm install
npm run chrome    # Start Chrome, open https://chatgpt.com/ in browser and log in
```

## Commands

| Command | Description |
|---------|-------------|
| `npm run run` | **One-shot run**: Chrome → API → Agent (recommended) |
| `npm run run -- your task` | One-shot run with task, e.g. `npm run run -- find 10 today news and put on desktop` |
| `npm run chrome` | Start Chrome only (log in at chatgpt.com) |
| `npm run api` | Start API server only (port 3000) |
| `npm run agent` | Start agent only (Chrome + API must be running first) |

## Agent

- Connects to ChatGPT web via Chrome; AI plans and executes tasks locally (e.g. write files, run commands, push code).
- **Self-learning**: learned skills are saved as skills to avoid forgetting.
- Use **`/new`** to start a new conversation (e.g. `npm run agent "/new your task"` or type `/new task` in interactive mode).
- Example: `npm run agent "push this repo to branch AgentPilotCN"`; output goes to terminal; some tasks (e.g. "10 news today") write a txt on the desktop.

## Known issues

**`npm run run`** auto-detects and starts Chrome + API. If you still get errors after running it:

- **Chrome was closed** → Run `npm run chrome` again and confirm you are logged in at https://chatgpt.com/ in the browser.
- **API service died** → Restart with `npm run api`.
- **Multiple Chrome instances** → Close all Chrome windows, then run `npm run run` again.

**After a system/PC restart**, you may see:
```text
Error: AgentPilot request failed (500): {"ok":false,"error":"Protocol error: Connection closed."}
```
The CDP connection to Chrome is gone. Run `npm run chrome` (or `npm run run`) again so Chrome starts with the debug port, then confirm you are logged in at https://chatgpt.com/ before using the agent.

## Roadmap

- [ ] IDE integration for API (e.g. Cursor)
- [ ] Vision module
- [ ] Voice input

## License

MIT
