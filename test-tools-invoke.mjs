/**
 * 测试 POST /tools/invoke 和 xml-tools 解析
 * 运行: node test-tools-invoke.mjs
 */
import http from 'http';
import * as xmlTools from './src/xml-tools.js';

// 1. 测试 xml-tools 解析
console.log('=== xml-tools 解析测试 ===');
const invokeText = '<function_calls><invoke name="run_command"><parameter name="command">ls</parameter></invoke></function_calls>';
console.log('invoke 格式:', JSON.stringify(xmlTools.parseToolCalls(invokeText), null, 2));

const toolCallText = '<tool_call name="Bash">{"command":"pwd"}</tool_call>';
console.log('tool_call 格式:', JSON.stringify(xmlTools.parseToolCalls(toolCallText), null, 2));

const execText = '<tool_call name="exec">{"command":"dir"}</tool_call>';
console.log('exec 格式:', JSON.stringify(xmlTools.parseToolCalls(execText), null, 2));

console.log('stripToolCalls:', xmlTools.stripToolCalls(toolCallText + ' hello'));

// 2. 测试 /tools/invoke（需先启动 api-server）
console.log('\n=== POST /tools/invoke 测试 ===');
const body = JSON.stringify({ tool: 'run_command', args: { command: process.platform === 'win32' ? 'echo ok' : 'echo ok' } });
const req = http.request({
  hostname: '127.0.0.1',
  port: 3000,
  path: '/tools/invoke',
  method: 'POST',
  headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
}, (res) => {
  let data = '';
  res.on('data', c => data += c);
  res.on('end', () => {
    console.log('run_command:', res.statusCode, data);
    const execBody = JSON.stringify({ tool: 'exec', args: { command: 'echo exec' } });
    const req2 = http.request({
      hostname: '127.0.0.1',
      port: 3000,
      path: '/tools/invoke',
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(execBody) },
    }, (res2) => {
      let d = '';
      res2.on('data', c => d += c);
      res2.on('end', () => console.log('exec (OpenClaw 兼容):', res2.statusCode, d));
    });
    req2.on('error', e => console.log('exec 测试跳过 (API 未启动):', e.message));
    req2.write(execBody);
    req2.end();
  });
});
req.on('error', (e) => console.log('API 未启动，跳过 HTTP 测试:', e.message));
req.write(body);
req.end();
