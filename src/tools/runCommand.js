/**
 * 通用命令执行 - run_command（兼容 function_calls）
 * script: 纯文本 PowerShell 脚本，工具层做 Base64 编码后执行，无需 AI 转义
 * command: 直接执行的命令字符串
 */
import execCommand from '../executor/commandExecutor.js';
import logger from '../logs/logger.js';

export async function runCommand(tool) {
  const { script, command } = tool?.args ?? {};
  if (script && String(script).trim()) {
    const raw = String(script);
    const realScript = raw
      .replace(/\\n/g, '\n')
      .replace(/; +/g, '\n');
    logger({ event: 'run_command_script', rawPreview: raw.slice(0, 80), hasNewline: realScript.includes('\n') });
    const encoded = Buffer.from(realScript, 'utf16le').toString('base64');
    const cmd = `powershell -EncodedCommand ${encoded}`;
    return execCommand(cmd);
  }
  const cmd = command ?? '';
  if (!cmd.trim()) throw new Error('command or script is required');
  return execCommand(cmd);
}
