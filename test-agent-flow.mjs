/**
 * 自动化测试 - 2阶段优化架构
 * 1. 单元测试：parser、processLlmOutput、工具执行
 * 2. E2E：bridge.chat（需 Chrome + 登录）
 */
import { parseTool } from './src/llm/parser.js';
import { processLlmOutput } from './src/router.js';
import bridge from './src/bridge.js';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

function loadConfig() {
  const raw = fs.readFileSync(path.join(__dirname, 'config.json'), 'utf-8');
  return JSON.parse(raw);
}

async function runAgent(userInput) {
  return bridge.chat(userInput);
}

// ========== 单元测试 ==========
function testToolParser() {
  console.log('\n--- 1. <tool> 解析 ---');
  const cases = [
    ['<tool>\nname: open_notepad\n</tool>', 'open_notepad', {}],
    ['<tool>\nname: list_dir\nargs: {"path":"."}\n</tool>', 'list_dir', { path: '.' }],
    ['<tool>\nname: run_command\nargs: {"command":"echo ok"}\n</tool>', 'run_command', { command: 'echo ok' }],
    ['<tool> name: run_command args: {"command":"echo test"} </tool>', 'run_command', { command: 'echo test' }],
    ['<tool>\nname: run_command\nargs: {"script":"Write-Output ok"}\n</tool>', 'run_command', { script: 'Write-Output ok' }],
  ];
  let ok = 0;
  for (const [xml, expectName, expectArgs] of cases) {
    const tool = parseTool(xml);
    const pass = tool && tool.name === expectName && JSON.stringify(tool.args) === JSON.stringify(expectArgs);
    console.log(pass ? '  ✓' : '  ✗', expectName, pass ? '' : `got ${JSON.stringify(tool)}`);
    if (pass) ok++;
  }
  return ok === cases.length;
}

function testFunctionCallsParser() {
  console.log('\n--- 2. <function_calls> 兼容解析 ---');
  const xml = '<function_calls><invoke name="list_dir"><parameter name="path">.</parameter></invoke></function_calls>';
  const tool = parseTool(xml);
  const pass = tool && tool.name === 'list_dir' && tool.args?.path === '.';
  console.log(pass ? '  ✓' : '  ✗', 'list_dir', pass ? '' : `got ${JSON.stringify(tool)}`);
  return pass;
}

async function testToolExecution() {
  console.log('\n--- 3. 工具执行（processLlmOutput）---');
  const projectDir = __dirname;

  const listOut = await processLlmOutput(`<tool>\nname: list_dir\nargs: {"path":"${projectDir.replace(/\\/g, '\\\\')}"}\n</tool>`);
  const hasEntries = listOut.ok && Array.isArray(listOut.result) && listOut.result.length > 0;
  console.log('  list_dir .:', hasEntries ? `✓ ${listOut.result.length} 项` : '✗', listOut.error || '');

  const cmdOut = await processLlmOutput(`<tool>\nname: run_command\nargs: {"command":"echo ok"}\n</tool>`);
  const cmdOk = cmdOut.ok && String(cmdOut.result).includes('ok');
  console.log('  run_command (command):', cmdOk ? '✓' : '✗', cmdOut.result || cmdOut.error);

  const scriptOut = await processLlmOutput(`<tool>\nname: run_command\nargs: {"script":"Write-Output 'script-ok'"}\n</tool>`);
  const scriptOk = scriptOut.ok && String(scriptOut.result).includes('script-ok');
  console.log('  run_command (script):', scriptOk ? '✓' : '✗', scriptOut.result || scriptOut.error);

  return hasEntries && cmdOk && scriptOk;
}

// ========== E2E ==========
async function testViewPathE2E() {
  console.log('\n--- 4. E2E：查看目录 ---');
  const result = await runAgent(`查看 ${__dirname}`);
  const ok = result.ok && (Array.isArray(result.result) || typeof result.result === 'string');
  const hasContent = ok || (result.raw && result.raw.length > 0);
  console.log('  查看路径:', hasContent ? '✓' : '✗', result.error || (result.result?.slice?.(0, 60) ?? ''));
  return hasContent;
}

async function testOpenNotepadE2E() {
  console.log('\n--- 5. E2E：打开记事本 ---');
  if (process.platform !== 'win32') {
    console.log('  跳过（非 Windows）');
    return true;
  }
  const result = await runAgent('打开记事本');
  const ok = result.ok || (result.raw && result.raw.length > 0);
  console.log('  打开记事本:', ok ? '✓' : '✗', result.error || '');
  return ok;
}

async function main() {
  console.log('ZeroChatgpt 2阶段优化 测试');
  const e2e = process.argv.includes('--e2e');

  let passed = 0;
  let total = 3;

  if (testToolParser()) passed++;
  if (testFunctionCallsParser()) passed++;
  if (await testToolExecution()) passed++;

  if (e2e) {
    total = 5;
    try {
      if (await testViewPathE2E()) passed++;
      if (await testOpenNotepadE2E()) passed++;
    } catch (e) {
      console.log('  E2E 失败:', e.message);
      console.log('  提示: npm run chrome，登录 chatgpt.com 后运行 node test-agent-flow.mjs --e2e');
    }
  } else {
    console.log('\n  (跳过 E2E，加 --e2e 运行)');
  }

  console.log(`\n--- 结果: ${passed}/${total} 通过 ---\n`);
  process.exit(passed === total ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
