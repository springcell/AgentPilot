# AgentPilot

用 ChatGPT 网页版驱动本地对话，无需 API Key。纯对话模式。

## 环境部署

### 环境要求

- Node.js 18+（若未安装，见下方命令行安装或 [Node.js 官网](https://nodejs.org/) 下载，安装后需**重新打开终端**再执行后续步骤）
- Chrome 浏览器
- Windows（PowerShell 脚本）

**命令行安装 Node.js（任选其一）：**

```powershell

winget install OpenJS.NodeJS.LTS --source winget
 
 #加入环境
$env:Path = "C:\Program Files\nodejs;" + $env:Path
```

### 安装步骤

```bash
# 1. 克隆项目
git clone https://github.com/springcell/AgentPilot.git
cd AgentPilot

# 2. 安装依赖
npm install

# 3. 启动 Chrome 并登录 ChatGPT
npm run chrome
```

启动 Chrome 后，在浏览器中打开 https://chatgpt.com/ 并登录。

## npm run 使用

| 命令 | 说明 |
|------|------|
| `npm start` | 启动服务。无参数时启动 HTTP（3000 端口）；带参数时执行单次对话 |
| `npm run chrome` | 启动 Chrome 调试模式，用于连接 ChatGPT 网页 |
| `npm run api` | 仅启动 API 服务 |
| `npm run run` | 一键运行：依次启动 Chrome → API → 智能体 |
| `npm run check` | 检查 CDP 连接是否正常 |
| `npm run auth` | 认证并缓存 cookie |
| `npm run ngrok` | 启动 ngrok 隧道（用于 Cursor 公网接入） |
| `npm run debug` | 调试模式运行 |
| `npm run chat` | 纯对话模式（不执行工具） |
| `npm run mcp` | 单独启动 MCP 服务 |

### 使用示例

```bash
# 单次对话
npm start 你好，请介绍一下你自己

# 交互模式（持续对话）
npm start

# 带参数的一键运行
npm run run -- 你好
```

## AI 智能体执行器

`agent/` 目录包含 Windows AI 智能体，通过网页桥驱动 ChatGPT 规划并本地执行任务，无需 API Key。

```powershell
# 1. 启动 Chrome 并登录 chatgpt.com
npm run chrome

# 2. 运行智能体（自动启动桥接 API）
npm run agent
npm run agent "帮我找今天10条新闻放在桌面"
```

详见 [agent/README.md](agent/README.md)。

## Cursor 接入说明

当前 API 服务兼容 **OpenAI 格式**（`/v1/chat/completions`），可被 Cursor 当作自定义模型使用。 
Cursor 的请求会经 `api2.cursor.sh` 转发，因此 **Base URL 必须是公网可访问的地址**，本机 `http://127.0.0.1:3000` 无法被 Cursor 云端访问。

### 步骤一：本地服务 + 公网隧道

1. **启动 Chrome 并登录 ChatGPT**（若未做过）：
   ```bash
   npm run chrome
   ```
   在浏览器中打开 https://chatgpt.com/ 并登录。

2. **启动 API 服务**（默认 3000 端口）：
   ```bash
   npm run api
   ```

3. **暴露公网隧道**（二选一）：
   - **推荐：ngrok**
     将 [ngrok](https://ngrok.com/download) 的 `ngrok.exe` 放到项目根目录或 `ngrok/` 下。 
     **首次使用**需注册并配置 authtoken（免费）：[注册](https://dashboard.ngrok.com/signup) → [获取 authtoken](https://dashboard.ngrok.com/get-started/your-authtoken) → 在项目目录执行：
     ```powershell
     .
grok
grok.exe config add-authtoken 你的authtoken
     ```
     然后启动隧道：
     ```bash
     npm run ngrok
     ```
     或直接：`.
grok
grok.exe http 3000`。终端会显示类似 `https://xxxx.ngrok-free.app` 的地址，记下它。
   - 其他：用任何可把本机 3000 端口映射到公网 HTTPS 的工具（如 cloudflared、frp 等），得到你的 **HTTPS Base URL**。

### 步骤二：在 Cursor 中配置

1. 打开 Cursor：**Settings → Cursor Settings → Models**（或 **Features → OpenAI API** 等，以你当前版本为准）。
2. 添加 **Custom OpenAI-compatible** 或 **OpenAI API** 类型的模型：
   - **Base URL**：填 `https://你的公网地址/v1` 
     例如 ngrok 为 `https://abcd1234.ngrok-free.app` 时，填 `https://abcd1234.ngrok-free.app/v1`。
   - **API Key**：随意填一个非空即可（例如 `ollama`），服务端不校验。
3. 选择该模型后即可在 Cursor 中通过 AgentPilot 使用 ChatGPT 网页版能力。

### API 说明（供排查）

| 用途           | 路径                     | 说明 |
|----------------|--------------------------|------|
| Cursor / 兼容层 | `POST /v1/chat/completions` | OpenAI 格式，支持 `messages`，可选 `stream` |
| 模型列表       | `GET /v1/models`         | 返回 gpt-4o / gpt-4 等模型 ID |
| 智能体对话     | `POST /chat`             | 供 agent_loop 等内部使用 |

本地仅监听 `127.0.0.1:3000`，不直接对外网开放，需通过 ngrok 等隧道访问。

---

## 计划

- [ ] HTTP API 文档（/chat、/v1/chat/completions）
- [ ] config.json 配置说明

## License

MIT