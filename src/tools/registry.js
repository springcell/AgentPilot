/**
 * 工具注册表 - 2阶段优化格式
 */
import * as openApp from './openApp.js';
import * as fileTools from './fileTools.js';
import * as systemTools from './systemTools.js';
import * as runCommand from './runCommand.js';
import { askUserConfirm } from './confirmTool.js';

export default {
  open_notepad: () => openApp.openNotepad(),
  open_cmd: () => openApp.openCMD(),
  list_dir: (tool) => fileTools.listDir(tool?.args),
  read_file: (tool) => fileTools.readFile(tool?.args),
  write_file: (tool) => fileTools.writeFile(tool?.args),
  sys_info: () => systemTools.systemInfo(),
  run_command: (tool) => runCommand.runCommand(tool),
  confirm: (tool) => askUserConfirm(tool?.args?.message ?? '是否继续？'),
};
