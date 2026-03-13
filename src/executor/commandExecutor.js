/**
 * 命令执行器 - 经 commandGuard 后执行
 */
import { exec } from 'child_process';
import { promisify } from 'util';
import commandGuard from '../security/commandGuard.js';

const execAsync = promisify(exec);

export default function execCommand(cmd) {
  return new Promise((resolve, reject) => {
    if (commandGuard(cmd)) {
      return reject(new Error('blocked command'));
    }

    let runCmd = cmd;
    if (process.platform === 'win32') {
      // cmd.exe 会把 ^ 当作转义符，导致 SendKeys('^s') 变成 SendKeys('s')，需转义为 ^^
      const escaped = String(cmd).replace(/\^/g, '^^');
      runCmd = `cmd.exe /d /s /c ${JSON.stringify(escaped)}`;
    }

    execAsync(runCmd, { timeout: 30000 })
      .then(({ stdout }) => resolve(stdout?.trim() ?? ''))
      .catch((err) => reject(err));
  });
}
