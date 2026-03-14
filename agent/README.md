# Windows AI 智能体执行器

通过 AgentPilot 网页桥驱动 ChatGPT，无需 API Key。AI 规划任务并输出 JSON 指令，本地执行器在 Windows 上执行。

## 项目结构

```
agent/
├── json_parser.py    # JSON 指令解析（多策略兜底）
├── executor.py       # 本地执行核心
├── agent_loop.py     # AI 闭环主程序（调用网页桥）
├── system_prompt.txt # 可编辑的系统提示
├── requirements.txt  # 依赖（本方案仅用标准库）
└── README.md
```

## 架构

```
用户输入任务
    ↓
agent_loop.py → AgentPilot 网页桥 (ChatGPT Web CDP)
    ↓ AI 返回含 JSON 的响应
executor.py → 提取 JSON 块 → 本地执行 (PowerShell/cmd/Python)
    ↓ 执行结果
agent_loop.py → 回传给 AI → AI 继续下一步
    ↓
直到 AI 输出 "✅ 任务完成" 或无更多 JSON 指令
```

## 前置条件

1. **Chrome 并登录 ChatGPT**（必须）：
   ```powershell
   npm run chrome
   ```
   在浏览器中打开 https://chatgpt.com/ 并登录。
2. **桥接 API**：`npm run agent` 会自动启动；或手动 `npm run api`。

## 运行

```powershell
# 推荐：一键运行（自动启动桥接 API）
npm run agent
npm run agent "帮我找今天10条美伊新闻放在桌面"

# 或直接调用 Python（需先 npm run api）
npm run agent:raw
python agent/agent_loop.py "任务描述"

# 只测试执行器（不调用 AI，手动粘贴 AI 响应）
python agent/executor.py
```

## 配置

- `AGENTPILOT_URL` 环境变量：默认 `http://127.0.0.1:3000/chat`
- 修改 `agent_loop.py` 中的 `SYSTEM_PROMPT` 可自定义 AI 行为（如限制操作范围）

## JSON 指令格式

```json
{
  "command": "powershell",
  "arguments": [
    "$desktop = [Environment]::GetFolderPath('Desktop')",
    "Write-Output '执行成功'"
  ]
}
```

支持的 command：`powershell`、`cmd`、`python`

## 安全提示

- 本程序会在本地执行 AI 生成的代码，请只对可信任务使用
- 建议在 SYSTEM_PROMPT 中明确限制可执行的操作范围
