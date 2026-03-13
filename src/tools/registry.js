/**
 * 工具注册表 - 2阶段优化格式
 */
import * as openApp from './openApp.js';
import * as fileTools from './fileTools.js';
import * as systemTools from './systemTools.js';
import * as runCommand from './runCommand.js';

export default {
  open_notepad: () => openApp.openNotepad(),
  open_cmd: () => openApp.openCMD(),
  list_dir: (tool) => fileTools.listDir(tool?.args),
  read_file: (tool) => fileTools.readFile(tool?.args),
  sys_info: () => systemTools.systemInfo(),
  run_command: (tool) => runCommand.runCommand(tool),
};
