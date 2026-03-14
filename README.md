# AgentPilot

用 ChatGPT 网页版驱动本地 AI Agent，无需 API Key。

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
| `npm start` | 启动 Agent。无参数时启动 HTTP 服务（3000 端口）；带参数时执行单次任务 |
| `npm run chrome` | 启动 Chrome 调试模式，用于连接 ChatGPT 网页 |
| `npm run api` | 仅启动 API 服务 |
| `npm run run` | 一键运行（自动检测并启动 Chrome） |
| `npm run check` | 检查 CDP 连接是否正常 |
| `npm run auth` | 认证并缓存 cookie |
| `npm run ngrok` | 启动 ngrok 隧道（用于 Cursor 公网接入） |
| `npm run test` | 运行 Agent 流程测试 |
| `npm run test:e2e` | 运行 E2E 测试 |
| `npm run debug` | 调试模式运行 |
| `npm run chat` | 纯对话模式（不执行工具） |
| `npm run mcp` | 单独启动 MCP 服务 |

### 使用示例

```bash
# 单次任务
npm start 查看 D:\Projects 目录下的文件

# 交互模式（持续对话）
npm start

# 带参数的一键运行
npm run run -- 你好，请介绍一下你自己
```

---

## 计划

以下功能与文档待完善：

- [ ] 核心能力说明（零 Token、多 Agent、本地工具、安全防护）
- [ ] 工作模式说明（intent / verify / single）
- [ ] HTTP API 文档（/chat、/agent、/tools/invoke）
- [ ] Cursor 接入（MCP 工具、API 替换主模型、mcp.json 配置）
- [ ] 内置工具列表与参数说明
- [ ] config.json 配置说明
- [ ] 故障排查指南

## License

MIT
