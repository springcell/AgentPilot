/**
 * 路径白名单策略 - 文件相关工具必须限制在允许根目录内
 */
import path from 'path';

/**
 * 规范化路径并检查是否在允许根目录内
 * @param {string} inputPath - 用户输入的路径
 * @param {string[]} allowedRoots - 允许的根目录列表
 * @returns {string} 规范化后的安全路径
 * @throws {Error} 若路径不在白名单内
 */
export function normalizeAndCheckPath(inputPath, allowedRoots) {
  if (!inputPath || typeof inputPath !== 'string') {
    throw new Error('Path is required');
  }
  const resolved = path.resolve(inputPath);
  const normalized = path.normalize(resolved);

  const ok = (allowedRoots || []).some((root) => {
    const safeRoot = path.normalize(path.resolve(root));
    return normalized.toLowerCase().startsWith(safeRoot.toLowerCase());
  });

  if (!ok) {
    throw new Error(`Path not allowed: ${normalized}`);
  }

  return normalized;
}
