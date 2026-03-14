/**
 * 系统工具 - systemInfo
 */
import os from 'os';

const PLATFORM_NAMES = { win32: 'Windows', darwin: 'macOS', linux: 'Linux' };

export async function systemInfo() {
  const platform = os.platform();
  return {
    platform,
    platformName: PLATFORM_NAMES[platform] ?? platform,
    cpu: os.cpus().length,
    memory: Math.round(os.totalmem() / 1024 / 1024) + ' MB',
  };
}
