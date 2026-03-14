/**
 * 命令执行器 - 经 commandGuard 后执行
 */
import { exec } from 'child_process';
import commandGuard from '../security/commandGuard.js';

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

    exec(runCmd, { timeout: 30000 }, (err, stdout, stderr) => {
      if (err) {
        const msg = [err.message];
        if (stdout) msg.push('stdout: ' + String(stdout).trim().slice(0, 500));
        if (stderr) msg.push('stderr: ' + String(stderr).trim().slice(0, 500));
        return reject(new Error(msg.filter(Boolean).join('\n')));
      }
      resolve(stdout?.trim() ?? '');
    });
  });
}
