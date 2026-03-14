/**
 * Router - 解析 LLM 输出、安检、调用工具
 */
import { parseTool } from './llm/parser.js';
import registry from './tools/registry.js';
import promptGuard from './security/promptGuard.js';
import logger from './logs/logger.js';

/**
 * 处理 llm_output，返回 { ok, result? } 或 { error }
 */
export async function processLlmOutput(llm_output) {
  if (promptGuard(llm_output)) {
    logger({ event: 'blocked', reason: 'prompt_injection' });
    return { error: 'prompt injection blocked' };
  }

  const tool = parseTool(llm_output);

  if (!tool) {
    logger({ event: 'no_tool', llm_output: String(llm_output).slice(0, 200) });
    return { error: 'no tool' };
  }

  const handler = registry[tool.name];

  if (!handler) {
    logger({ event: 'unknown_tool', tool: tool.name });
    return { error: 'unknown tool' };
  }

  try {
    const result = await handler(tool);
    logger({ event: 'tool_ok', tool: tool.name });
    return { ok: true, result };
  } catch (e) {
    const errParts = [e.message];
    if (e.stdout) errParts.push('stdout: ' + String(e.stdout).slice(0, 500));
    if (e.stderr) errParts.push('stderr: ' + String(e.stderr).slice(0, 500));
    const fullError = errParts.filter(Boolean).join('\n');
    logger({ event: 'tool_error', tool: tool.name, error: e.message });
    return { ok: false, error: fullError };
  }
}

export default async function router(req, res) {
  const { llm_output } = req.body || {};
  const out = await processLlmOutput(llm_output);
  res.json(out);
}
