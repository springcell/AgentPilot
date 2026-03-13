/**
 * LLM 输出解析 - 支持 <tool> 与 <function_calls> 格式
 */
export function parseTool(text) {
  if (!text || typeof text !== 'string') return null;

  // 格式 1: <tool>name: xxx\nargs: {...}</tool> 或单行 <tool> name: run_command args: {...} </tool>
  const toolMatch = text.match(/<tool>([\s\S]*?)<\/tool>/i);
  if (toolMatch) {
    const block = toolMatch[1];
    const nameMatch = block.match(/name:\s*([^\s\n,]+)/);
    let name = nameMatch ? nameMatch[1].trim() : null;
    // 若 name 被错误合并了 args（如 "run_command args: {...}"），只取工具名
    if (name && name.includes(' args:')) {
      name = name.split(' args:')[0].trim();
    }
    let args = {};
    const argsMatch = block.match(/args:\s*(\{[\s\S]*\})/);
    if (argsMatch) {
      try {
        args = JSON.parse(argsMatch[1].trim());
      } catch (_) {}
    }
    if (name) return { name: mapToolName(name), args };
  }

  // 格式 2: <function_calls><invoke name="xxx">...</invoke></function_calls>
  const fnMatch = text.match(/<function_calls>([\s\S]*?)<\/function_calls>/i);
  if (fnMatch) {
    const block = fnMatch[1];
    const invokeMatch = block.match(/<invoke\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/invoke>/i);
    if (invokeMatch) {
      const toolName = invokeMatch[1].trim();
      const paramsXml = invokeMatch[2] || '';
      const args = {};
      const paramReg = /<parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)<\/parameter>/gi;
      let pm;
      while ((pm = paramReg.exec(paramsXml)) !== null) {
        args[pm[1].trim()] = (pm[2] || '').trim();
      }
      const mapped = mapToolName(toolName);
      return { name: mapped, args };
    }
  }

  return null;
}

function mapToolName(name) {
  const key = (name || '').toLowerCase();
  const m = {
    open_notepad: 'open_notepad',
    open_cmd: 'open_cmd',
    list_dir: 'list_dir',
    read_file: 'read_file',
    sys_info: 'sys_info',
    run_command: 'run_command',
  };
  return m[key] ?? name;
}
