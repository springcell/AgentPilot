/**
 * 命令黑名单 - 拦截危险命令
 * @param {string} cmd - 要执行的命令
 * @returns {boolean} true 表示应拦截
 */
export default function commandGuard(cmd) {
  const blocked = [
    'del ',
    'erase ',
    'rd ',
    'rmdir ',
    'format ',
    'shutdown',
    'reg add',
    'reg delete',
    'curl',
    'wget',
    '&&',
    '||',
    '|',
    '>',
  ];

  const lower = (cmd || '').toLowerCase();
  return blocked.some((v) => lower.includes(v));
}
