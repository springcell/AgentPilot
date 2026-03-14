#!/usr/bin/env node
/** 调试模式启动：跳过路径白名单和命令黑名单检查 */
process.env.DEBUG_MODE = 'true';
await import('../src/agent.js');
