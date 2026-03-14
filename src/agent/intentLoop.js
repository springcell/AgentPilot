/**
 * Intent Loop - 多身份多对话架构
 * Thinker(分析) -> Extractor(提取JSON) -> 执行 -> Verifier(验收)
 * 每个 Agent 独立 Tab，职责单一，互不干扰
 */
import * as chatgptWeb from '../chatgpt-web-client.js';
import { processLlmOutput } from '../router.js';
import promptGuard from '../security/promptGuard.js';
import logger from '../logs/logger.js';
import { AGENTS } from './agents.js';
import { askUserConfirm } from '../tools/confirmTool.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', '..', 'config.json');

function loadAgentConfig() {
  try {
    const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
    const cfg = JSON.parse(raw);
    return cfg?.agent ?? {};
  } catch (_) {
    return {};
  }
}

function parseJSON(text) {
  const fenceMatch = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenceMatch) {
    try {
      return JSON.parse(fenceMatch[1].trim());
    } catch (_) {}
  }
  const jsonLabelMatch = text.match(/JSON\s*\n(\{[\s\S]*\})/);
  if (jsonLabelMatch) {
    try {
      return JSON.parse(jsonLabelMatch[1].trim());
    } catch (_) {}
  }
  const bareMatch = text.match(/\{[\s\S]*?"(?:actions|passed|isDependencyIssue)"[\s\S]*\}/);
  if (bareMatch) {
    try {
      return JSON.parse(bareMatch[0].trim());
    } catch (_) {}
  }
  return null;
}

async function executeTool(toolName, args) {
  const fakeTool = `<tool>\nname: ${toolName}\nargs: ${JSON.stringify(args || {})}\n</tool>`;
  const out = await processLlmOutput(fakeTool);
  return { ok: !out.error, result: out.result, error: out.error };
}

async function executeWithFallback(action) {
  const out = await executeTool(action.tool, action.args);
  if (out.ok) return out;

  console.log(`[Fallback] 主命令失败: ${out.error?.slice(0, 100)}`);

  for (const fb of action.fallback ?? []) {
    console.log(`[Fallback] 尝试: ${fb.args?.command ?? fb.args?.script ?? fb.tool}`);
    const fbOut = await executeTool(fb.tool, fb.args);
    if (fbOut.ok) {
      console.log(`[Fallback] 成功`);
      return fbOut;
    }
    console.log(`[Fallback] 失败: ${fbOut.error?.slice(0, 80)}`);
  }

  return { ok: false, error: `All fallbacks exhausted for ${action.tool}` };
}

const RESULT_MAX_LEN = 800;

const initializedAgents = new Set();

async function callAgent(agentName, message, cdpOpts, isFirst = false) {
  const agent = AGENTS[agentName];
  const needsInit = !initializedAgents.has(agentName);
  const useFirst = isFirst || needsInit;
  if (useFirst) initializedAgents.add(agentName);
  const fullMessage = useFirst ? `${agent.prompt}\n\n---\n\n${message}` : message;

  return chatgptWeb.chat(fullMessage, {
    ...cdpOpts,
    agentId: agent.id,
    newChat: isFirst,
  });
}

function buildExtractorPrompt(userGoal, thinkText, extraHint = '') {
  return `
用户目标：${userGoal}

AI分析：${thinkText}

请只输出 JSON。

Windows run_command 规则：
- run_command 默认可能通过 cmd.exe 执行。
- 如果要使用 PowerShell 语法，必须显式调用：
  powershell -NoProfile -ExecutionPolicy Bypass -Command "..."
- 不要直接输出裸 PowerShell 命令，例如：
  Get-WmiObject、Get-ChildItem、Get-Content、$env:...
- 查询已安装软件时，不要优先使用：
  Get-WmiObject -Class Win32_Product

Windows 查询已安装软件推荐策略：
- 优先读取以下卸载注册表项：
  HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*
  HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*
  HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*
- 若失败，fallback 使用 winget list
- 若仍失败，再尝试 wmic product get name

输出要求：
- answer: 给用户的简短说明
- actions: 动作数组
- 每个动作必须包含 tool 和 args
- 如果主命令可能失败，应提供 fallback
- 命令优先选择稳定、低副作用、兼容性更好的方案

示例仅供参考：
{
  "answer": "我来帮你查看电脑上已安装的软件。",
  "actions": [
    {
      "tool": "run_command",
      "args": {
        "command": "powershell -NoProfile -ExecutionPolicy Bypass -Command \\"Get-ItemProperty 'HKLM:\\\\Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\*','HKLM:\\\\Software\\\\WOW6432Node\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\*','HKCU:\\\\Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\*' | Select-Object DisplayName,DisplayVersion,Publisher | Where-Object { $_.DisplayName } | Sort-Object DisplayName\\""
      },
      "fallback": [
        { "tool": "run_command", "args": { "command": "winget list" } },
        { "tool": "run_command", "args": { "command": "wmic product get name" } }
      ]
    }
  ]
}

${extraHint}
`.trim();
}

function buildUserResult(intent, verdict, actionResults) {
  const outputs = actionResults
    .filter((x) => x.ok && x.result)
    .map((x) => `【${x.tool} 输出】\n${String(x.result).slice(0, 4000)}`);

  return [
    intent?.answer || '',
    verdict?.reason ? `\n执行结论：${verdict.reason}` : '',
    outputs.length ? `\n\n执行结果：\n${outputs.join('\n\n')}` : '',
  ]
    .join('')
    .trim() || 'Done';
}

async function executeActionWithInstaller(action, baseChatOpts) {
  let out = await executeWithFallback(action);
  if (!out.ok) {
    console.log('[Installer] 检查是否依赖问题...');
    const installRes = await callAgent(
      'installer',
      `失败命令：${JSON.stringify(action)}\n错误：${out.error}`,
      baseChatOpts,
      false
    );
    const installPlan = parseJSON(installRes?.text || '');
    if (installPlan?.isDependencyIssue && installPlan?.installActions?.length) {
      if (installPlan.requiresUserConfirm) {
        const confirmed = await askUserConfirm(installPlan.confirmMessage ?? '是否继续安装？');
        if (!confirmed) return { ok: false, error: '用户取消安装' };
      }
      for (const ia of installPlan.installActions ?? []) {
        await executeTool(ia.tool, ia.args);
      }
      console.log('[Execute] 安装后重试:', action.tool);
      out = await executeWithFallback(action);
    } else if (!installPlan?.isDependencyIssue && installPlan?.alternativeCommand) {
      console.log('[Installer] 非依赖问题，用替代命令重试:', installPlan.alternativeCommand);
      out = await executeWithFallback({
        tool: 'run_command',
        args: { command: installPlan.alternativeCommand },
        fallback: [],
      });
    }
  }
  return out;
}

export async function runIntentLoop(userGoal, options = {}) {
  const cfg = loadAgentConfig();
  const multiAgent = options.multiAgent ?? cfg.multiAgent ?? true;
  const cdp = options.cdp ?? {};

  const baseChatOpts = {
    cdpUrl: cdp.url ?? 'http://127.0.0.1:9222',
    chatgptUrl: cdp.chatgptUrl ?? 'https://chatgpt.com/',
    pollIntervalMs: cdp.replyPollIntervalMs ?? 500,
    pollTimeoutMs: cdp.replyPollTimeoutMs ?? 120000,
    pageReadyTimeoutMs: cdp.pageReadyTimeoutMs ?? 15000,
  };

  if (multiAgent) {
    return runMultiAgentLoop(userGoal, baseChatOpts, options);
  }
  return runSingleAgentLoop(userGoal, baseChatOpts, options);
}

async function runMultiAgentLoop(userGoal, baseChatOpts, options) {
  try {
    console.log('[SysInfo] 获取系统信息...');
    const sysOut = await executeTool('sys_info', {});
    const sysContext = sysOut.ok
      ? (typeof sysOut.result === 'object' ? JSON.stringify(sysOut.result, null, 2) : String(sysOut.result))
      : 'Windows (unknown version)';

    console.log('[Agent:Thinker] 开始分析...');
    const thinkRes = await callAgent(
      'thinker',
      `系统环境：${sysContext}\n\n目标：${userGoal}`,
      baseChatOpts,
      true
    );
    const thinkText = thinkRes?.text || '';
    console.log('[Agent:Thinker] 完成:', thinkText?.slice(0, 100));

    console.log('[Agent:Extractor] 提取JSON...');
    const extractRes = await callAgent(
      'extractor',
      buildExtractorPrompt(userGoal, thinkText),
      baseChatOpts,
      true
    );
    console.log('[Agent:Extractor] 完成:', extractRes?.text?.slice(0, 200));

    const intent = parseJSON(extractRes?.text || '');
    console.log('[Intent]', JSON.stringify(intent, null, 2));

    if (!intent?.actions) {
      return { ok: false, error: 'Extraction failed: no valid JSON' };
    }

    const actionResults = [];
    for (const action of intent.actions ?? []) {
      const { tool } = action;
      if (!tool) continue;
      console.log(`[Execute] ${tool}`, action.args);
      const out = await executeActionWithInstaller(action, baseChatOpts);
      if (out.error === '用户取消安装') {
        return { ok: false, error: out.error };
      }
      actionResults.push({
        tool,
        ok: out.ok,
        result:
          typeof out.result === 'object'
            ? JSON.stringify(out.result, null, 2).slice(0, RESULT_MAX_LEN)
            : String(out.result ?? '').slice(0, RESULT_MAX_LEN),
        error: out.error,
      });
      if (!out.ok) {
        console.log(`[Execute] 跳过，继续下一步`);
      }
    }

    console.log('[Agent:Verifier] 验收...');
    const verifyRes = await callAgent(
      'verifier',
      `用户目标：${userGoal}\n执行结果：${JSON.stringify(actionResults, null, 2)}`,
      baseChatOpts,
      true
    );
    console.log('[Agent:Verifier] 完成:', verifyRes?.text?.slice(0, 200));

    const verdict = parseJSON(verifyRes?.text || '');
    console.log('[Verdict]', verdict);

    if (verdict?.passed) {
      return { ok: true, result: buildUserResult(intent, verdict, actionResults) };
    }

    if (verdict?.next) {
      console.log('[Retry] Extractor 重试...');
      const retryRes = await callAgent(
        'extractor',
        buildExtractorPrompt(
          userGoal,
          thinkText,
          `上次失败：${verdict.reason}\n建议：${verdict.next}\n请重新提取 JSON，并继续遵守上述规则。`
        ),
        baseChatOpts,
        false
      );
      const retryIntent = parseJSON(retryRes?.text || '');
      if (retryIntent?.actions?.length) {
        const retryResults = [];
        for (const action of retryIntent.actions) {
          const out = await executeActionWithInstaller(action, baseChatOpts);
          if (out.error === '用户取消安装') {
            return { ok: false, error: out.error };
          }
          retryResults.push({
            tool: action.tool,
            ok: out.ok,
            result:
              typeof out.result === 'object'
                ? JSON.stringify(out.result, null, 2).slice(0, RESULT_MAX_LEN)
                : String(out.result ?? '').slice(0, RESULT_MAX_LEN),
            error: out.error,
          });
        }
        const retryVerifyRes = await callAgent(
          'verifier',
          `用户目标：${userGoal}\n执行结果：${JSON.stringify(retryResults, null, 2)}`,
          baseChatOpts,
          false
        );
        const retryVerdict = parseJSON(retryVerifyRes?.text || '');
        return retryVerdict?.passed
          ? { ok: true, result: buildUserResult(retryIntent, retryVerdict, retryResults) }
          : { ok: false, error: retryVerdict?.reason ?? 'Retry failed' };
      }
    }

    return { ok: false, error: verdict?.reason ?? 'Verification failed' };
  } catch (e) {
    if (e.message?.startsWith('CF_BLOCKED')) {
      return {
        ok: false,
        error: e.message.replace('CF_BLOCKED:', '').trim(),
        code: 'CF_BLOCKED',
      };
    }
    throw e;
  } finally {
    initializedAgents.clear();
    await chatgptWeb.closeAllAgents();
    await chatgptWeb.disconnectBrowser();
  }
}

const INTENT_BASE = `User goal: {userGoal}

Think freely, then output a JSON code block with the actions needed:

\`\`\`json
{
  "answer": "你的自然语言回答（可选）",
  "actions": [
    {"tool": "write_file", "args": {"path": "$env:USERPROFILE\\\\Desktop\\\\file.txt", "content": "..."}},
    {"tool": "run_command", "args": {"command": "start notepad path"}}
  ],
  "verify": {"tool": "list_dir", "args": {"path": "$env:USERPROFILE\\\\Desktop"}, "expect": "filename"}
}
\`\`\`

Tools: open_notepad, open_cmd, list_dir, read_file, write_file, sys_info, run_command.`;

function buildIntentPrompt(userGoal, retryReason = null) {
  const base = INTENT_BASE.replace('{userGoal}', userGoal);
  if (!retryReason) return base;
  if (retryReason.type === 'parse') {
    return `Your last response did not contain a valid JSON code block. Please output JSON:\n\n` + base;
  }
  if (retryReason.type === 'action') {
    return `Action failed:\nTool: ${retryReason.tool}\nArgs: ${JSON.stringify(retryReason.args ?? {})}\nError: ${retryReason.got}\n\nRetry with corrected action.\n\n` + base;
  }
  if (retryReason.type === 'verify') {
    return `Verification FAILED. Expected: ${retryReason.expect}\nGot: ${retryReason.got}\n\nFix and retry.\n\n` + base;
  }
  return base;
}

async function runVerify(intent) {
  const { tool, args, expect } = intent.verify || {};
  if (!tool || !expect) return { passed: true };
  const allowedTools = ['read_file', 'list_dir', 'run_command'];
  if (!allowedTools.includes(tool)) return { passed: false, got: '', expect };
  const out = await executeTool(tool, args);
  const resultStr = out.ok
    ? (typeof out.result === 'object' ? JSON.stringify(out.result) : String(out.result))
    : String(out.error ?? '');
  const passed = resultStr.includes(expect);
  return { passed, got: resultStr.slice(0, 500), expect };
}

async function runSingleAgentLoop(userGoal, baseChatOpts, options) {
  const cfg = loadAgentConfig();
  const maxRetries = options.intentRetries ?? cfg.intentRetries ?? 3;
  const totalTimeoutMs = options.verifyLoopTimeoutMs ?? cfg.verifyLoopTimeoutMs ?? 120000;

  const start = Date.now();
  let retryReason = null;

  for (let attempt = 0; attempt < maxRetries; attempt++) {
    if (Date.now() - start > totalTimeoutMs) {
      return { ok: false, error: 'Intent loop timeout' };
    }

    const prompt = buildIntentPrompt(userGoal, retryReason);
    const response = await chatgptWeb.chat(prompt, {
      ...baseChatOpts,
      newChat: true,
    });
    const text = response?.text || '';

    if (promptGuard(text)) {
      logger({ event: 'intent_loop_blocked', reason: 'prompt_injection' });
      return { ok: false, error: 'prompt injection blocked' };
    }

    const intent = parseJSON(text);
    if (!intent) {
      console.log('[intent] parse failed, raw text:', text.slice(0, 500));
      retryReason = { type: 'parse', got: text.slice(0, 200) };
      continue;
    }

    retryReason = null;
    for (const action of intent.actions ?? []) {
      const { tool } = action;
      if (!tool) continue;
      const out = await executeActionWithInstaller(action, baseChatOpts);
      if (out.error === '用户取消安装') {
        return { ok: false, error: out.error };
      }
      if (!out.ok) {
        retryReason = { type: 'action', tool, args: action.args, got: out.error ?? String(out.result ?? '') };
        break;
      }
    }
    if (retryReason) continue;

    if (intent.verify) {
      const v = await runVerify(intent);
      if (!v.passed) {
        retryReason = { type: 'verify', expect: v.expect, got: v.got };
        continue;
      }
    }

    return { ok: true, result: intent.answer ?? 'Done' };
  }

  return {
    ok: false,
    error: retryReason ? `Failed after ${maxRetries} retries: ${retryReason.type}` : `Max retries (${maxRetries}) reached`,
  };
}
