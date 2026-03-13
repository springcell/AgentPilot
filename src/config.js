/**
 * 执行上下文配置 - allowedRoots（供 fileTools 等使用）
 * 2阶段优化：config.json 由各模块直接读取
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_PATH = path.join(__dirname, '..', 'config.json');

function loadConfig() {
  const raw = fs.readFileSync(CONFIG_PATH, 'utf-8');
  return JSON.parse(raw);
}

export function createExecutionContext(sessionId = 'local-session', userId = 'local-user') {
  const config = loadConfig();
  const exec = config.execution || {};
  return {
    sessionId,
    userId,
    allowedRoots: exec.allowedRoots || [process.cwd(), '.'],
  };
}
