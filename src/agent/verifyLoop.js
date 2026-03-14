/**
 * Agent verify loop - goal-driven execution with verification and retry
 */
import * as chatgptWeb from '../chatgpt-web-client.js';
import { parseAgentStep } from '../llm/parser.js';
import { processLlmOutput } from '../router.js';
import promptGuard from '../security/promptGuard.js';
import logger from '../logs/logger.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'config.json');

let _agentConfigCache = null;
function loadAgentConfig() {
  if (_agentConfigCache) return _agentConfigCache;
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
    const cfg = JSON.parse(raw);
    _agentConfigCache = cfg?.agent ?? {};
  } catch (_) {
    _agentConfigCache = {};
  }
  return _agentConfigCache;
}

const PLAN_PROMPT = `User goal: {userGoal}

Output a JSON plan. Format:
{
  "steps": ["step1 description", "step2 description"],
  "verify": {
    "tool": "read_file" | "list_dir" | "run_command",
    "args": {"path": "..."} or {"script": "..."} etc,
    "expect": "substring that must appear in result (e.g. filename, content snippet)"
  }
}

Verify tool: read_file for content, list_dir for file existence, run_command for other checks.
Output ONLY the JSON, no explanation.`;

const VERIFY_PROMPT = `You are a task execution Agent. You MUST verify each step before claiming success.

Rules:
1. Output <tool>...</tool> for each action. Include verify: "criterion" in your reasoning.
2. After receiving a tool result, either output next <tool> to verify/retry, or <status>success</status> if verified.
3. Prefer read_file, list_dir for verification. Use run_command for execution.
4. Never output success without verification.

Execution: (1) NEVER call notepad/mspaint in script - they block. Use separate run_command command: "start notepad path" after file exists. (2) Invoke-WebRequest: -TimeoutSec, try/catch, output SUCCESS:path or FAILED:msg. (3) For listing installed software use registry (avoids winget progress bar): Get-ItemProperty 'HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*','HKLM:\\Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*' | Select-Object DisplayName,DisplayVersion | Where-Object {$_.DisplayName} | Sort-Object DisplayName | Select-Object -First 60 | Format-Table -AutoSize. If winget is needed add --disable-interactivity and pipe through Select-Object -First 60.

Termination: Same goal fails 3 times (FAILED prefix or 404) -> output <status>failed</status> with reason. Do NOT keep retrying. URL 404 -> do not guess other URLs.

Web scraping: Use known entry URLs only (e.g. https://news.sina.com.cn/world/). Never guess URLs. If Invoke-WebRequest fails (blocked/403/timeout), do NOT retry. Instead output <result> with your own knowledge about the topic, then <status>success</status>. Only output <status>failed</status> when you cannot answer at all.

read_file path: Use $env:USERPROFILE or C:\\\\Users\\\\... (tool expands $env:).

For analysis/summary tasks (e.g. summarize news, market trend): after gathering data with tools, output <result>你的分析内容</result> then <status>success</status>. Do NOT output plain text only.

CRITICAL: If the goal contains 保存/桌面/desktop/save/txt/md: You MUST call write_file tool FIRST. NEVER output <result> without calling write_file before it. If you already know the answer, still call write_file to save it, then output <result>.

For tasks that require saving to file: (1) First use write_file to save. (2) Verify with read_file. (3) Only then output <result><status>success</status>. When saving: put write_file FIRST, read_file verification SECOND.

Tools: open_notepad, open_cmd, list_dir, read_file, write_file, sys_info, run_command.

ONLY use this exact format for tools:
<tool>
name: write_file
args: {"path":"$env:USERPROFILE\\\\Desktop\\\\file.txt","content":"内容"}
</tool>

NEVER use write_file(...) or read_file(...) function call syntax. JSON args only. No exceptions.

Output: <tool> for actions; <result>content</result> for analysis answers; <status>success</status> or <status>failed</status> for termination. No explanation outside tags.`;

function parsePlan(text) {
  const blockMatch = text.match(/```(?:json)?\s*(\{[\s\S]*?\})\s*```/);
  const rawMatch = text.match(/\{[\s\S]*?"steps"[\s\S]*?"verify"[\s\S]*\}/);
  const jsonStr = blockMatch?.[1] ?? rawMatch?.[0];
  if (jsonStr) {
    try {
      const plan = JSON.parse(jsonStr);
      if (plan?.verify?.tool && plan?.verify?.args && plan?.verify?.expect) {
        return plan;
      }
    } catch (_) {}
  }
  return null;
}

async function executeVerify(plan) {
  const { tool, args, expect } = plan.verify;
  const allowedTools = ['read_file', 'list_dir', 'run_command'];
  if (!allowedTools.includes(tool)) return { passed: false, error: `Invalid verify tool: ${tool}` };
  const fakeTool = `<tool>\nname: ${tool}\nargs: ${JSON.stringify(args)}\n</tool>`;
  const out = await processLlmOutput(fakeTool);
  const resultStr = out.ok
    ? (typeof out.result === 'object' ? JSON.stringify(out.result) : String(out.result))
    : String(out.error ?? '');
  const passed = resultStr.includes(expect);
  return { passed, got: resultStr.slice(0, 500), expect };
}

function buildFollowUp(toolName, result, verify) {
  const ok = result?.ok !== false;
  const resultStr = result?.result != null ? String(result.result) : (result?.error || '');
  return `Tool: ${toolName}
Result: ${ok ? 'OK' : 'FAIL'}: ${resultStr.slice(0, 500)}
Verify: ${verify || 'N/A'}

Did it pass? Output next <tool> to verify/retry, or <status>success</status> if done.`;
}

export async function runVerifyLoop(userGoal, options = {}) {
  const cfg = loadAgentConfig();
  const maxRounds = options.maxRounds ?? cfg.maxRounds ?? 10;
  const sameTaskRetries = options.sameTaskRetries ?? cfg.sameTaskRetries ?? 3;
  const failLimit = options.failLimit ?? cfg.failLimit ?? 3;
  const totalTimeoutMs = options.verifyLoopTimeoutMs ?? cfg.verifyLoopTimeoutMs ?? 120000;
  const planVerify = options.planVerify ?? cfg.planVerify ?? false;
  const cdp = options.cdp ?? {};

  const start = Date.now();

  // 获取系统环境信息，注入所有 Agent 上下文，解决 Verifier 猜错 OS 的问题
  let sysInfoStr = '';
  try {
    const sysOut = await processLlmOutput('<tool>\nname: sys_info\n</tool>');
    if (sysOut.ok) {
      sysInfoStr = typeof sysOut.result === 'string'
        ? sysOut.result
        : JSON.stringify(sysOut.result);
    }
  } catch (_) {}
  const sysContext = sysInfoStr ? `[系统环境]\n${sysInfoStr}\n\n` : '';
  let round = 0;
  let plan = null;
  let lastVerify = null;
  let sameTaskRetryCount = 0;
  let failCount = 0;
  let lastToolName = null;
  let lastResult = null;

  const baseChatOpts = {
    cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
    chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
    pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
    pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
    pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
  };

  let prompt = planVerify && round === 0
    ? PLAN_PROMPT.replace('{userGoal}', userGoal)
    : sysContext + VERIFY_PROMPT + '\n\nUser goal: ' + userGoal;

  while (round < maxRounds) {
    if (Date.now() - start > totalTimeoutMs) {
      return { ok: false, error: 'Agent timeout' };
    }

    const response = await chatgptWeb.chat(prompt, { ...baseChatOpts, newChat: round === 0 && (options.newChat ?? false) });
    const text = response?.text || '';

    if (promptGuard(text)) {
      logger({ event: 'verify_loop_blocked', reason: 'prompt_injection' });
      return { ok: false, error: 'prompt injection blocked' };
    }

    if (planVerify && round === 0) {
      plan = parsePlan(text);
      if (!plan) {
        prompt = 'Invalid plan. Output valid JSON with steps and verify (tool, args, expect).\n\n' + PLAN_PROMPT.replace('{userGoal}', userGoal);
        round++;
        continue;
      }
      prompt = VERIFY_PROMPT + '\n\nUser goal: ' + userGoal + '\n\nYour plan: ' + JSON.stringify(plan.verify);
      round++;
      continue;
    }

    const step = parseAgentStep(text);

    if (step.tool) {
      const toolName = step.tool.name;
      const verify = step.verify || '';

      if (verify && verify === lastVerify) {
        sameTaskRetryCount++;
        if (sameTaskRetryCount >= sameTaskRetries) {
          return { ok: false, error: `Same task failed ${sameTaskRetries} times: ${verify}` };
        }
      } else {
        sameTaskRetryCount = 0;
      }
      lastVerify = verify;
      lastToolName = toolName;

      const toolBlock = text.match(/<tool>[\s\S]*?<\/tool>/i)?.[0];
      const llmOutput = toolBlock || text;

      const out = await processLlmOutput(llmOutput);

      if (out.error && out.error === 'no tool') {
        prompt = buildFollowUp(toolName, { ok: false, error: out.error }, verify) + '\n\n(No tool parsed. Retry with valid <tool> format.)';
        round++;
        continue;
      }

      lastResult = out.ok ? out.result : out.error;
      const resultStr = String(lastResult ?? '');
      if (resultStr.startsWith('FAILED') || resultStr.includes('404')) {
        failCount++;
        if (failCount >= failLimit) {
          return { ok: false, error: `Failed ${failLimit} times: ${resultStr.slice(0, 200)}` };
        }
      } else {
        failCount = 0;
      }
      prompt = buildFollowUp(toolName, out, verify);
      round++;
      continue;
    }

    if (step.result && !step.tool) {
      const needsSave = /桌面|desktop|保存|储存|txt|md|save|store/i.test(userGoal);
      if (needsSave) {
        const nameMatch = userGoal.match(/(\S+)\.(txt|md)/i);
        const fileName = nameMatch ? nameMatch[0] : 'result.md';
        const fakeTool = `<tool>\nname: write_file\nargs: ${JSON.stringify({
          path: `$env:USERPROFILE\\Desktop\\${fileName}`,
          content: step.result,
        })}\n</tool>`;
        await processLlmOutput(fakeTool);
      }
      if (plan) {
        const v = await executeVerify(plan);
        if (!v.passed) {
          prompt = `Verification FAILED.\nExpected: ${v.expect}\nGot: ${v.got}\nRetry the task.\n\n` + prompt;
          round++;
          continue;
        }
      }
      return { ok: true, result: step.result };
    }

    if (step.status === 'success') {
      if (plan) {
        const v = await executeVerify(plan);
        if (!v.passed) {
          prompt = `Verification FAILED.\nExpected: ${v.expect}\nGot: ${v.got}\nRetry the task.\n\n` + prompt;
          round++;
          continue;
        }
      }
      return { ok: true, result: lastResult ?? 'Verified' };
    }

    if (step.status === 'failed') {
      return { ok: false, error: lastResult ?? 'Task failed' };
    }

    const chineseContent = text.match(/[\u4e00-\u9fa5][^<]{20,}/g)?.join('\n');
    if (chineseContent && chineseContent.length > 50) {
      return { ok: true, result: chineseContent };
    }

    const fallbackHint = lastToolName
      ? buildFollowUp(lastToolName, { ok: false, error: 'no tag found' }, lastVerify)
      : `User goal: ${userGoal}`;

    prompt = fallbackHint + '\n\nNo valid tag found. Output:\n- <tool>...</tool> to execute\n- <result>内容</result><status>success</status> if you have the answer\n- <status>failed</status> if truly stuck';
    round++;
    continue;
  }

  return { ok: false, error: `Max rounds (${maxRounds}) reached` };
}
