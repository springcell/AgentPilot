/**
 * 系统工具 - systemInfo
 */
import os from 'os';

export async function systemInfo() {
  return {
    platform: os.platform(),
    cpu: os.cpus().length,
    memory: os.totalmem(),
  };
}
