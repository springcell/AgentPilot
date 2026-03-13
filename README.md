# ZeroChatgpt

通过 Chrome CDP 调用 ChatGPT 网页版，无需 API Key。

## Cursor 接入

### 方式 A：MCP 工具（推荐，无循环）

将 ChatGPT 网页版作为 **工具** 供 Cursor Agent 调用，主模型仍用 Cursor 内置，避免循环。

1. 启动 Chrome：`npm run chrome`，登录 https://chatgpt.com/
2. 项目已配置 `.cursor/mcp.json`，重启 Cursor 后自动加载
3. 在 Agent 对话中说「用 ask_chatgpt 工具问 ChatGPT：xxx」，或让 Agent 自行选择调用

### 方式 B：API 替换主模型（需 ngrok）

**重要**：Cursor 的请求会先经过其后端 `api2.cursor.sh`，因此 Base URL 必须是**公网可访问**的。本地 `127.0.0.1` 无法被 Cursor 后端访问，需用 **ngrok** 暴露。

**若出现无限循环**：请改用 **Chat 模式**（非 Agent），或使用方式 A（MCP 工具）。

### 步骤

1. **安装 ngrok**：https://ngrok.com/download 或 `winget install ngrok.ngrok`

2. **启动服务**（开 3 个终端）：
   ```bash
   # 终端 1：Chrome
   npm run chrome
   # 在浏览器打开 https://chatgpt.com/ 并登录

   # 终端 2：API 服务
   npm run api

   # 终端 3：ngrok 隧道
   npm run ngrok
   ```

3. **复制 ngrok 的 HTTPS 地址**（如 `https://xxxx.ngrok-free.dev` 或 `.ngrok-free.app`），在末尾加 `/v1`

4. **在 Cursor 中配置**：
   - Settings → Models → API Keys → OpenAI
   - 开启 **Override OpenAI Base URL**
   - Base URL：`https://xxxx.ngrok-free.app/v1`（用你的 ngrok 地址）
   - API Key：`ollama` 或 `sk-zero-chatgpt`
   - 勾选模型：**gpt-4o**、gpt-4 等

5. **每次重启 ngrok 后**，Base URL 会变，需在 Cursor 中更新。

## 快速开始

### 1. 安装依赖（已完成）

```bash
npm install
```

### 2. 启动 Chrome 调试模式

**重要：先关闭所有 Chrome 窗口**，然后执行：

```bash
npm run chrome
```

启动后，在浏览器中打开 https://chatgpt.com/ 并登录。

### 3. 运行

**一键运行（自动检测并启动 Chrome）：**
```bash
npm run run
npm run run -- 你好，请介绍一下你自己
```

**或直接运行 Agent：**
```bash
npm start
npm start 写一首短诗
```

**本地快速路径（无需 ChatGPT）：**
- 读桌面文件：`npm start 告诉我桌面上 Ckey.txt 的内容`
- 查看目录：`npm start 查看 D:\path\to\dir`

**认证（可选，缓存 cookie）：**
```bash
npm run auth
```

**检查 CDP 连接：**
```bash
npm run check
```

## XML 工具（OpenClaw 风格）

在 `config.json` 的 `tools` 中配置工具后，CLI Agent 会向 ChatGPT 注入 XML 格式描述，并解析回复中的 `<function_calls><invoke name="...">` 或 `<tool_call name="...">` 执行工具。

格式示例：
```xml
<function_calls>
  <invoke name="tool_name">
    <parameter name="arg">value</parameter>
  </invoke>
</function_calls>
```

### 如何让 Web 调用工具

| 方式 | 适用场景 | 步骤 |
|------|----------|------|
| **CLI Agent** | 终端对话，ChatGPT 网页版驱动 | `npm run chrome` + `npm start 查看 D:\path` |
| **HTTP API** | 网页/脚本直接调工具 | 启动 `npm run api`，网页 `fetch` 到 `/tools/invoke` |
| **MCP** | Cursor 内用 ask_chatgpt | 配置 `.cursor/mcp.json`，Agent 说「用 ask_chatgpt 问…」 |

#### 方式 1：CLI Agent（ChatGPT 网页版 + 工具）

1. 启动 Chrome：`npm run chrome`，登录 https://chatgpt.com/
2. 运行：`npm start 查看 D:\Other\WOWunity\ZeroChatgpt`
3. Agent 会把问题发给 ChatGPT 网页，解析回复中的 `<invoke>` / `<tool_call>`，在本地执行工具并返回结果

#### 方式 2：网页直接调用 /tools/invoke

1. 启动 API：`npm run api`（默认 http://127.0.0.1:3000）
2. 网页中发起请求：

```javascript
// 浏览器或 Node 中
const res = await fetch('http://127.0.0.1:3000/tools/invoke', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ tool: 'run_command', args: { command: 'dir' } }),
});
const data = await res.json();  // { ok: true, result: { stdout, stderr } }
```

若网页与 API 不同域，需用 ngrok 暴露：`npm run ngrok`，将 `http://127.0.0.1:3000` 换成 ngrok 的 HTTPS 地址。

**内置演示页**：启动 API 后访问 http://127.0.0.1:3000/tools-demo 可在浏览器中直接测试工具调用。

#### 方式 3：MCP（Cursor 内用 ChatGPT 当工具）

MCP 的 `ask_chatgpt` 只负责把问题发给 ChatGPT 并返回回复，**不执行** run_command 等工具。若要在 Cursor 内用工具，需用 **方式 1** 的 CLI Agent，或自行在网页/脚本中调用 **方式 2** 的 `/tools/invoke`。

### POST /tools/invoke 接口说明

```bash
curl -X POST http://127.0.0.1:3000/tools/invoke \
  -H "Content-Type: application/json" \
  -d '{"tool":"run_command","args":{"command":"ls -la"}}'
```

支持工具名：`run_command`、`read_file`、`write_file`、`list_dir`。OpenClaw 的 `exec`、`Bash` 会映射为 `run_command`。

### 网页 GPT 工具调用链路测试

验证「用户输入 → ChatGPT 网页返回 → 解析为工具调用 → 本地执行」全流程：

```bash
npm run chrome          # 终端 1：启动 Chrome，登录 chatgpt.com
npm run test:e2e        # 终端 2：运行 E2E 测试
```

E2E 包含：解释工程（list_dir + ChatGPT 解释）、打开 CMD（发送「打开cmd窗口」→ 解析返回 → 执行 start cmd）。

## 配置

编辑 `config.json`：

| 配置项 | 说明 |
|--------|------|
| `llm.cdp.url` | CDP 地址，默认 `http://127.0.0.1:9222` |
| `llm.cdp.chromePath` | Chrome 可执行文件路径（若自动检测失败） |
| `llm.cdp.chatgptUrl` | ChatGPT 网页地址 |

## 故障排查

**"please check the api-key" 报错：**
- **必须使用 ngrok**：`127.0.0.1` 无法被 Cursor 后端访问
- 运行 `npm run ngrok`，用 ngrok 的 HTTPS 地址 + `/v1` 作为 Base URL
- API Key 填 `ollama`

**模型列表为空：**
- 确认 Base URL 为 ngrok 的 HTTPS 地址（非 localhost）
- 重启 Cursor 后重试

**ngrok 浏览器警告页：** ngrok 免费版可能对 API 请求显示警告页。若 Cursor 仍报错，可尝试 Cloudflare Tunnel：`cloudflared tunnel --url http://127.0.0.1:3000`

## 自定义 Chrome 路径

若系统未自动找到 Chrome，在 `config.json` 中设置：

```json
"cdp": {
  "chromePath": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
}
```
