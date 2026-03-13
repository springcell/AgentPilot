/**
 * 应用工具 - openNotepad, openCMD
 */
import execCommand from '../executor/commandExecutor.js';

export async function openNotepad() {
  return execCommand('start powershell -Command "Start-Process notepad"');
}

export async function openCMD() {
  return execCommand('start cmd');
}
