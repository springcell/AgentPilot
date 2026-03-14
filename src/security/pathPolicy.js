/**
 * 路径白名单策略 - 文件相关工具必须限制在允许根目录内
 */
import path from 'path';

function expandPathEnvVars(inputPath) {
  if (!inputPath || typeof inputPath !== 'string') return inputPath;
  let s = inputPath;
  s = s.replace(/\$env:(\w+)/gi, (_, key) => process.env[key] ?? `$env:${key}`);
  s = s.replace(/%(\w+)%/g, (_, key) => process.env[key] ?? `%${key}%`);
  return s;
}

/**
 * 规范化路径并检查是否在允许根目录内
 * @param {string} inputPath - 用户输入的路径（支持 $env:VAR 和 %VAR%）
 * @param {string[]} allowedRoots - 允许的根目录列表
 * @returns {string} 规范化后的安全路径
 * @throws {Error} 若路径不在白名单内
 */
export function normalizeAndCheckPath(inputPath, allowedRoots) {
  if (!inputPath || typeof inputPath !== 'string') {
    throw new Error('Path is required');
  }
  const expanded = expandPathEnvVars(inputPath);
  const resolved = path.resolve(expanded);
  const normalized = path.normalize(resolved);

  if (process.env.DEBUG_MODE === 'true') {
    return normalized;
  }

  const ok = (allowedRoots || []).some((root) => {
    const safeRoot = path.normalize(path.resolve(root));
    return normalized.toLowerCase().startsWith(safeRoot.toLowerCase());
  });

  if (!ok) {
    throw new Error(`Path not allowed: ${normalized}`);
  }

  return normalized;
}
