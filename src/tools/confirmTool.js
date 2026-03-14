/**
 * 用户确认工具 - 交互式确认窗口
 */
import { createInterface } from 'readline';

export async function askUserConfirm(message) {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout });
    rl.question(`\n⚠️  ${message} (y/n)> `, (answer) => {
      rl.close();
      resolve(answer.trim().toLowerCase() === 'y' || answer.trim().toLowerCase() === 'yes');
    });
  });
}
