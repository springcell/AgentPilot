# AgentPilot

**用 ChatGPT 网页版驱动本地 AI Agent，无需 API Key。** 通过 Chrome CDP 控制 ChatGPT，解析其回复并执行本地工具（读文件、运行命令、打开应用等）。

## 核心能力

| 能力 | 说明 |
|------|------|
| **零 Token 成本** | 使用 ChatGPT 网页版，不消耗 API 额度 |
| **多 Agent 协作** | Thinker 分析 → Extractor 提取动作 → 执行 → Verifier 验收 |
| **本地工具执行** | 读/写文件、列目录、运行命令、打开记事本/CMD、系统信息 |
| **安全防护** | 命令白名单、路径限制、提示注入检测 |
| **多种接入方式** | CLI、HTTP API、Cursor MCP |

## 快速开始

### 1. 安装依赖

```bash
npm install
```

### 2. 启动 Chrome 并登录 ChatGPT

```bash
npm run chrome
```

启动后，在浏览器中打开 https://chatgpt.com/ 并登录。

### 3. 运行 Agent

```bash
# 单次任务
npm start 查看 D:\Projects 目录下的文件

# 交互模式（持续对话）
npm start
```

**常用示例：**
- `npm start 告诉我桌面上 xxxx.txt 的内容`
- `npm start 打开记事本`
- `npm start 列出 D:\Other 下的文件夹`

## 工作模式

在 `config.json` 的 `agent.mode` 中配置：

| 模式 | 说明 |
|------|------|
| `intent` | 多 Agent 协作（默认）：分析 → 提取 JSON 动作 → 执行 → 验收 |
| `verify` | 简化版：单轮工具调用 + 验收循环 |
| `single` | 单轮：直接解析工具调用，无验收 |

## HTTP API

启动服务：`npm start`（无参数时监听 3000 端口）

| 接口 | 说明 |
|------|------|
| `POST /chat` | 发送消息，由 ChatGPT 驱动并执行工具 |
| `POST /agent` | 直接传入 `llm_output`，解析并执行工具 |
| `POST /tools/invoke` | 直接调用工具（不经过 LLM） |
| `GET /health` | 健康检查 |

```bash
# 对话
curl -X POST http://127.0.0.1:3000/chat -H "Content-Type: application/json" -d '{"message":"列出 C:\\Users 目录"}'

# 直接调用工具
curl -X POST http://127.0.0.1:3000/tools/invoke -H "Content-Type: application/json" -d '{"tool":"list_dir","args":{"path":"C:\\Users"}}'
```

## Cursor 接入

### 方式 A：MCP 工具（推荐）

将 ChatGPT 作为工具供 Cursor Agent 调用，主模型仍用 Cursor 内置。

1. 启动 Chrome：`npm run chrome`，登录 chatgpt.com
2. 项目已配置 `.cursor/mcp.json`，重启 Cursor 后自动加载
3. 在 Agent 中说「用 ask_chatgpt 问 ChatGPT：xxx」

#### `.cursor/mcp.json` 配置说明

MCP（Model Context Protocol）让 Cursor 能调用本项目的 `ask_chatgpt` 工具。配置示例：

```json
{
  "mcpServers": {
    "zero-chatgpt": {
      "command": "node",
      "args": ["src/mcp-server.js"]
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `mcpServers` | MCP 服务列表，key 为服务名（如 `zero-chatgpt`） |
| `command` | 启动命令，使用 `node` 运行 MCP 服务 |
| `args` | 命令参数，`src/mcp-server.js` 为项目内的 MCP 入口 |

**注意：**

- 路径 `src/mcp-server.js` 相对于**项目根目录**（即 `package.json` 所在目录）
- Cursor 会在打开该工作区时自动以项目根目录为 `cwd` 执行上述命令
- 若项目不在工作区根目录，需加 `cwd` 指定工作目录，例如：

```json
{
  "mcpServers": {
    "zero-chatgpt": {
      "command": "node",
      "args": ["src/mcp-server.js"],
      "cwd": "D:/Other/WOWunity/ZeroChatgpt"
    }
  }
}
```

**验证：** 重启 Cursor 后，在 Agent 中提及「用 ask_chatgpt 问…」，Agent 应能调用该工具。若未生效，检查 Chrome 是否已启动并登录 chatgpt.com。

### 方式 B：API 替换主模型（需 ngrok）

Cursor 请求会经过其后端，Base URL 必须公网可访问。本地需用 ngrok 暴露：

```bash
# 终端 1
npm run chrome   # 登录 chatgpt.com

# 终端 2
npm start        # 启动 API

# 终端 3
npm run ngrok    # 复制 HTTPS 地址，末尾加 /v1
```

在 Cursor：Settings → Models → Override OpenAI Base URL → 填入 ngrok 地址 + `/v1`，API Key 填 `ollama`。

## 内置工具

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件内容 |
| `write_file` | 写入文件 |
| `list_dir` | 列出目录 |
| `run_command` | 执行命令或 PowerShell 脚本 |
| `open_notepad` | 打开记事本 |
| `open_cmd` | 打开 CMD |
| `sys_info` | 系统信息 |

工具执行受 `config.json` 中 `execution.allowedRoots` 和 `commandWhitelist` 限制。

## 配置

`config.json` 主要字段：

| 配置项 | 说明 |
|--------|------|
| `agent.mode` | 工作模式：intent / verify / single |
| `agent.multiAgent` | 是否启用多 Agent（Thinker/Extractor/Verifier） |
| `llm.cdp.url` | CDP 地址，默认 `http://127.0.0.1:9222` |
| `llm.cdp.chatgptUrl` | ChatGPT 网页地址 |
| `execution.allowedRoots` | 允许访问的根目录 |
| `execution.commandWhitelist` | 允许的命令白名单 |

## 故障排查

| 问题 | 处理 |
|------|------|
| 连接失败 / timeout | 先执行 `npm run chrome`，确保已登录 chatgpt.com |
| CDP 不可用 | `npm run check` 检查连接 |
| Cursor Base URL 报错 | 必须用 ngrok 公网地址，不能用 127.0.0.1 |
| 命令被拒绝 | 检查 `config.json` 的 `commandWhitelist` 和 `allowedRoots` |

## 脚本说明

| 命令 | 说明 |
|------|------|
| `npm start` | 启动 Agent（CLI 或 HTTP 服务） |
| `npm run chrome` | 启动 Chrome 调试模式 |
| `npm run api` | 仅启动 API 服务 |
| `npm run check` | 检查 CDP 连接 |
| `npm run auth` | 认证并缓存 cookie |
| `npm run ngrok` | 启动 ngrok 隧道 |
| `npm run test` | 运行 Agent 流程测试 |

## License

MIT
