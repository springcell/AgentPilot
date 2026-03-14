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

function tryParseJSON(raw) {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (_) {}

  let repaired = raw
    .replace(/"command"\s*:\s*"powershell([^"]*?) -Command "([^"]*?)""/g, (_, a, b) =>
      `"command":"powershell${a} -Command \\"${b}\\""`
    )
    .replace(/"command"\s*:\s*"tasklist \/FI "([^"]*?)""/g, (_, a) =>
      `"command":"tasklist /FI \\"${a}\\""`
    );

  // 修复 command 中未转义的反斜杠（如注册表路径 HKEY_LOCAL_MACHINE\SOFTWARE）
  repaired = repaired.replace(
    /"command"\s*:\s*"((?:[^"\\]|\\.)*)"/g,
    (match, content) => {
      const fixed = content.replace(/\\([^"\\/bfnrtu])/g, '\\\\$1');
      return `"command":"${fixed}"`;
    }
  );

  try {
    return JSON.parse(repaired);
  } catch (_) {
    return null;
  }
}

function parseJSON(text) {
  if (!text || typeof text !== 'string') return null;
  const raw = text.trim();

  const fenceMatch = raw.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenceMatch) return tryParseJSON(fenceMatch[1].trim());

  const jsonLabelMatch = raw.match(/JSON\s*\n(\{[\s\S]*\})/);
  if (jsonLabelMatch) return tryParseJSON(jsonLabelMatch[1].trim());

  const bareMatch = raw.match(/\{[\s\S]*?"(?:actions|passed|isDependencyIssue)"[\s\S]*\}/);
  if (bareMatch) return tryParseJSON(bareMatch[0].trim());

  const startIdx = raw.indexOf('{');
  if (startIdx >= 0) {
    let depth = 0;
    let inString = false;
    let escape = false;
    let endIdx = -1;
    for (let i = startIdx; i < raw.length; i++) {
      const c = raw[i];
      if (escape) {
        escape = false;
        continue;
      }
      if (c === '\\' && inString) {
        escape = true;
        continue;
      }
      if (inString) {
        if (c === '"' || c === "'") inString = false;
        continue;
      }
      if (c === '"' || c === "'") inString = true;
      else if (c === '{' || c === '[') depth++;
      else if (c === '}' || c === ']') {
        depth--;
        if (depth === 0) {
          endIdx = i;
          break;
        }
      }
    }
    if (endIdx >= 0) return tryParseJSON(raw.slice(startIdx, endIdx + 1));
    let candidate = raw.slice(startIdx);
    for (const suffix of ['}', ']}', ']}]}', '}]}']) {
      const parsed = tryParseJSON(candidate + suffix);
      if (parsed) return parsed;
    }
  }
  return null;
}

async function executeTool(toolName, args) {
  const fakeTool = `<tool>\nname: ${toolName}\nargs: ${JSON.stringify(args || {})}\n</tool>`;
  const out = await processLlmOutput(fakeTool);
  return { ok: !out.error, result: out.result, error: out.error };
}

let lastErrorFeedback = null;

function generateErrorDetails(error, context = {}) {
  const msg = error?.message ?? String(error ?? '');
  let suggestion = '检查命令中的引号是否转义、路径反斜杠是否转义为 \\\\，或尝试 fallback 中的替代命令。';
  if (msg.includes('权限') || msg.includes('Access') || msg.includes('拒绝')) {
    suggestion = '该操作可能需要管理员权限，或使用无需提权的替代命令。';
  } else if (msg.includes('JSON') || msg.includes('parse') || msg.includes('格式')) {
    suggestion = '检查 JSON 格式：command 内双引号转义为 \\"，路径反斜杠转义为 \\\\。';
  }
  return {
    message: msg,
    type: error?.name ?? 'Error',
    suggestion,
    ...context,
  };
}

function sendErrorFeedbackToAI(errorDetails) {
  lastErrorFeedback = errorDetails;
  console.log(`[ErrorFeedback] 错误反馈: ${JSON.stringify(errorDetails, null, 2)}`);
}

async function executeWithErrorFeedback(action) {
  try {
    const result = await executeTool(action.tool, action.args);
    if (!result.ok) throw new Error(result.error);
    return result;
  } catch (error) {
    const errorDetails = generateErrorDetails(error, { tool: action.tool, args: action.args });
    sendErrorFeedbackToAI(errorDetails);
    return {
      ok: false,
      error: errorDetails.message,
    };
  }
}

async function executeWithErrorHandling(action) {
  try {
    const result = await executeTool(action.tool, action.args);
    if (!result.ok) throw new Error(result.error);
    return result;
  } catch (error) {
    return { ok: false, error: error?.message ?? String(error) };
  }
}

async function requestPermissionForAction(action) {
  const confirmed = await askUserConfirm('该操作需要管理员权限，是否同意授权？');
  if (confirmed) {
    return await executeWithErrorFeedback(action);
  }
  return { ok: false, error: '用户拒绝授权，操作无法执行。' };
}

function getAlternativeAction(action) {
  const fb = action.fallback?.[0];
  return fb ? { tool: fb.tool, args: fb.args } : null;
}

async function handleExecutionError(error, action) {
  const errStr = typeof error === 'string' ? error : error?.message ?? String(error ?? '');
  if (errStr.includes('权限不足')) {
    return await requestPermissionForAction(action);
  }
  const alternativeAction = getAlternativeAction(action);
  if (alternativeAction) {
    const out = await executeWithErrorFeedback(alternativeAction);
    return out.ok ? { ...out, usedFallback: true } : out;
  }
  return { ok: false, error: errStr };
}

async function executeWithFallback(action) {
  const out = await executeTool(action.tool, action.args);
  if (out.ok) return out;

  const errors = [out.error];
  console.log(`[Fallback] 主命令失败: ${out.error?.slice(0, 100)}`);

  for (const fb of action.fallback ?? []) {
    console.log(`[Fallback] 尝试: ${fb.args?.command ?? fb.args?.script ?? fb.tool}`);
    const fbOut = await executeTool(fb.tool, fb.args);
    if (fbOut.ok) {
      console.log(`[Fallback] 成功`);
      return { ...fbOut, usedFallback: true };
    }
    errors.push(fbOut.error);
    console.log(`[Fallback] 失败: ${fbOut.error?.slice(0, 80)}`);
  }

  const errStr = errors.filter(Boolean).join(' | ').slice(0, 500);
  const errorDetails = generateErrorDetails(new Error(errStr), {
    tool: action.tool,
    args: action.args,
    suggestion: '所有 fallback 均已失败。请检查命令格式（引号、反斜杠转义），或提供其他替代方案。',
  });
  sendErrorFeedbackToAI(errorDetails);
  return { ok: false, error: `All fallbacks exhausted for ${action.tool}: ${errStr}` };
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
    newChat: useFirst,
  });
}

function buildExtractorPrompt(userGoal, thinkText, extraHint = '') {
  return `
用户目标：${userGoal}

AI分析：${thinkText}

你是JSON提取器。重要：只输出合法 JSON，不要输出解释文字，不要输出 markdown 代码块。

每个 action 必须包含 fallback 数组，提供 2-3 个备选方案。

Windows 命令规则：
1. 简单命令优先直接使用 cmd 可执行命令，例如：
   notepad, calc, tasklist, dir, winget, start
2. 仅当需要 PowerShell 专属语法时，才显式调用：
   powershell -NoProfile -ExecutionPolicy Bypass -Command "..."
3. 主 action、fallback、verify 中的所有 command 字符串都必须符合 JSON 语法：
   - 双引号必须转义为 \\"
   - 路径中的反斜杠必须转义为 \\\\（如注册表路径 HKEY_LOCAL_MACHINE\\\\SOFTWARE\\\\...）
4. 不要直接输出裸 PowerShell 命令，例如：
   Get-WmiObject、Get-ChildItem、Get-Content、$env:...
5. 查询已安装软件时，不要优先使用：
   Get-WmiObject -Class Win32_Product
6. 查询已安装软件时优先级：
   注册表卸载项 > winget > wmic(仅最后兜底)

格式：
{
  "answer": "自然语言回答（可选）",
  "actions": [
    {
      "tool": "工具名",
      "args": {},
      "fallback": [
        {"tool": "工具名", "args": {}},
        {"tool": "工具名", "args": {}}
      ]
    }
  ],
  "verify": {"tool": "验收工具", "args": {}, "expect": "期望字符串"}
}

可用工具:
write_file, read_file, list_dir, run_command, open_notepad, open_cmd, sys_info, confirm(args.message)

错误示例：
"command": "powershell -Command "Start-Process notepad""

正确示例：
"command": "powershell -Command \\"Start-Process notepad\\""

错误示例：
"command": "tasklist /FI "IMAGENAME eq notepad.exe""

正确示例：
"command": "tasklist /FI \\"IMAGENAME eq notepad.exe\\""

错误示例（注册表路径反斜杠未转义）：
"command": "reg query HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft..."

正确示例：
"command": "reg query HKEY_LOCAL_MACHINE\\\\SOFTWARE\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Uninstall\\\\*"

${extraHint}
`.trim();
}

const USER_RESULT_MAX_LEN = 7000;

function buildUserResult(intent, verdict, actionResults) {
  const outputs = actionResults
    .filter((x) => x.ok && x.result)
    .map((x) =>
      `【${x.tool}${x.usedFallback ? ' (fallback)' : ''} 输出】\n${String(x.result).slice(0, 4000)}`
    );

  let resultPart = outputs.length ? `\n\n执行结果：\n${outputs.join('\n\n')}` : '';
  if (resultPart.length > USER_RESULT_MAX_LEN) {
    resultPart = resultPart.slice(0, USER_RESULT_MAX_LEN) + '\n\n[输出过长，已省略]';
  }

  return [
    intent?.answer || '',
    verdict?.reason ? `\n执行结论：${verdict.reason}` : '',
    resultPart,
  ]
    .join('')
    .trim() || 'Done';
}

async function executeActionWithInstaller(action, baseChatOpts) {
  let out = await executeWithFallback(action);
  if (!out.ok) {
    const permissionRetry = await handleExecutionError(out.error, action);
    if (permissionRetry.ok) out = permissionRetry;
  }
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
      out = { ...out, usedFallback: true };
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
        usedFallback: out.usedFallback ?? false,
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
      const errorHint = [
        `上次失败：${verdict.reason}`,
        `建议：${verdict.next}`,
        lastErrorFeedback
          ? `\n执行错误详情（请据此修正 JSON）：\n${JSON.stringify(lastErrorFeedback, null, 2)}`
          : '',
        '请重新提取 JSON，并继续遵守上述规则。',
      ]
        .filter(Boolean)
        .join('\n');
      lastErrorFeedback = null;
      const retryRes = await callAgent(
        'extractor',
        buildExtractorPrompt(userGoal, thinkText, errorHint),
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
            usedFallback: out.usedFallback ?? false,
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
    lastErrorFeedback = null;
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
