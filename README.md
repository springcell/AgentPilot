# AgentPilot

用 ChatGPT 网页版驱动本地对话，无需 API Key。纯对话模式。

## 环境部署

### 环境要求

- Node.js 18+
- Chrome 浏览器
- Windows（PowerShell 脚本）

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
| `npm run run` | 一键运行（自动检测并启动 Chrome） |
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

## 计划

- [ ] HTTP API 文档（/chat、/v1/chat/completions）
- [ ] Cursor 接入说明
- [ ] config.json 配置说明

## License

MIT
