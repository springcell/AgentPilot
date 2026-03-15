# AgentPilot

用 ChatGPT 网页版驱动本地对话与任务执行的超轻量智能体，无需 API Key，无限随意使用！

**AgentPilot Studio** — A lightweight AI agent powered by ChatGPT web. No API, no token limits, unlimited use. It can write code, find files, and much more; explore what you can do with it.

---

## 环境要求
  Python 3 · 运行智能体（如 agent_loop.py、executor）需要。可从 python.org 下载，或用 winget install Python.Python.3.12 安装。

- **Node.js 18+** · [官网下载](https://nodejs.org/) 或命令行：
  ```powershell
  winget install OpenJS.NodeJS.LTS --source winget
  # 若终端找不到 npm，先执行：
  $env:Path = "C:\Program Files\nodejs;" + $env:Path
  ```
- **Chrome** 浏览器
- **Windows**（PowerShell）

## 安装与首次运行

```bash
git clone https://github.com/springcell/AgentPilot.git
cd AgentPilot
npm install
npm run chrome    # 启动 Chrome，在浏览器中打开 https://chatgpt.com/ 并登录
```

## 常用命令

| 命令 | 说明 |
|------|------|
| `npm run run` | **一键运行**：Chrome → API → 智能体（推荐） |
| `npm run run -- 你的任务` | 一键运行并下发任务，如 `npm run run -- 帮我找今天10条新闻放桌面` |
| `npm run chrome` | 仅启动 Chrome（需登录 chatgpt.com） |
| `npm run api` | 仅启动 API 服务（3000 端口） |
| `npm run agent` | 仅启动智能体（需先有 Chrome + API） |

## 智能体说明

- 通过 Chrome 连接 ChatGPT 网页，由 AI 规划并本地执行任务（如写文件、运行命令、推代码等）。
- 支持**自学习**：学会的技能会保存为 skill，避免遗忘。
- 示例：`npm run agent "把工程推送到子工程：AgentPilotCN"`，结果会输出到终端；部分任务（如「今天10条新闻」）会在桌面生成 txt，现在的中文子工程就是以上命令结果。
- 通过/new 开启新对话，或者重启智能体开启
- 如果报错 [1] AI thinking...
  Error: AgentPilot request failed (500): {"ok":false,"error":"Protocol error: Connection closed."}
  Chrome 被关掉了 → 重新运行 npm run chrome，在浏览器里确认 ChatGPT 已登
  再重启 npm run api
  
## 计划

- [ ] API 的 IDE 接入（如 Cursor）
- [ ] 视觉模块
- [ ] 语音输入

## License

MIT
