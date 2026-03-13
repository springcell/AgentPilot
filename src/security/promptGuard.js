/**
 * Prompt 注入防御 - 检测恶意指令
 * @param {string} text - LLM 输出或用户输入
 * @returns {boolean} true 表示检测到注入，应拦截
 */
export default function promptGuard(text) {
  const bad = [
    'ignore previous',
    'ignore all',
    'bypass',
    'delete system32',
    'format disk',
    'format c:',
  ];

  const lower = (text || '').toLowerCase();
  return bad.some((v) => lower.includes(v));
}
