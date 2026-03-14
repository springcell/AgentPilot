"""
3 轮模拟测试：验证 chat_via_bridge 完全避免卡死
使用 unittest.mock 模拟 /chat 和 /poll，不依赖真实 API
"""
import sys
import os
import json
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 测试用常量（缩短卡死检测时间）
TEST_STALL_SEC = 2
TEST_POLL_INTERVAL = 0.5
TEST_POLL_TIMEOUT = 10


def _make_chat_resp(result: str):
    return json.dumps({"ok": True, "result": result}).encode("utf-8")


def _make_poll_resp(text: str, generating: bool = False):
    return json.dumps({"ok": True, "text": text, "generating": generating}).encode("utf-8")


def test_round1_direct_completion():
    """第 1 轮：/chat 直接返回任务完成 → 不进入轮询，立即返回"""
    resp = _make_chat_resp("✅ 任务完成：已抓取新浪最新新闻并保存到桌面文本文件。")
    mock_resp = MagicMock()
    mock_resp.read.return_value = resp
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("agent_loop.urllib.request.urlopen", return_value=mock_resp):
        from agent_loop import chat_via_bridge

        out = chat_via_bridge("任务", new_chat=True)
        assert "✅ 任务完成" in out or "任务完成：" in out
        assert "新浪" in out or "新闻" in out

    print("✅ 第 1 轮：直接任务完成，无轮询，立即返回")


def test_round2_poll_then_completion():
    """第 2 轮：/chat 返回中间状态，/poll 第 2 次返回任务完成 → 轮询后返回"""
    chat_resp = _make_chat_resp("ChatGPT 说： 正在搜索网页")
    poll_responses = [
        _make_poll_resp("正在搜索", generating=True),
        _make_poll_resp("✅ 任务完成：已抓取新浪最新新闻并保存到桌面文本文件。", generating=False),
    ]
    poll_idx = [0]

    def mock_urlopen(req, timeout=None):
        mock = MagicMock()
        if "poll" in (getattr(req, "full_url", "") or str(req)):
            body = poll_responses[min(poll_idx[0], len(poll_responses) - 1)]
            poll_idx[0] += 1
        else:
            body = chat_resp
        mock.read.return_value = body
        mock.__enter__ = lambda s: s
        mock.__exit__ = lambda s, *a: None
        return mock

    # 需要 patch urlopen 能区分 chat 和 poll - urlopen 接收的是 Request 对象
    def urlopen_side_effect(req, timeout=None):
        mock = MagicMock()
        if isinstance(req, str) or (hasattr(req, "get_full_url") and "poll" in req.get_full_url()):
            body = poll_responses[min(poll_idx[0], len(poll_responses) - 1)]
            poll_idx[0] += 1
        else:
            body = chat_resp
        mock.read.return_value = body
        mock.__enter__ = lambda s: s
        mock.__exit__ = lambda s, *a: None
        return mock

    poll_calls = []

    def mock_http_get(url):
        poll_calls.append(1)
        n = len(poll_calls) - 1
        if n == 0:
            return {"ok": True, "text": "正在搜索", "generating": True}
        return {"ok": True, "text": "✅ 任务完成：已抓取新浪最新新闻并保存到桌面文本文件。", "generating": False}

    mock_resp = MagicMock()
    mock_resp.read.return_value = chat_resp
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("agent_loop.urllib.request.urlopen", return_value=mock_resp):
        with patch("agent_loop._http_get", side_effect=mock_http_get):
            with patch("agent_loop.time.sleep"):
                from agent_loop import chat_via_bridge
                out = chat_via_bridge("任务", new_chat=True)
        assert "✅ 任务完成" in out or "任务完成：" in out, f"out={out[:80]!r}"
        assert len(poll_calls) >= 1

    print("✅ 第 2 轮：中间状态 → 轮询 → 任务完成，正常返回")


def test_round3_stall_then_return():
    """第 3 轮：/chat 返回中间状态，/poll 一直返回中间状态 → 卡死检测后强制返回"""
    chat_resp = _make_chat_resp("ChatGPT 说： 正在搜索网页")
    poll_calls = []
    tick = [0]

    def mock_http_get(url):
        poll_calls.append(1)
        return {"ok": True, "text": "正在搜索", "generating": False}

    def mock_time():
        tick[0] += 1
        return 1000.0 + tick[0] * 5  # 每次调用 +5 秒

    mock_resp = MagicMock()
    mock_resp.read.return_value = chat_resp
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = lambda s, *a: None

    with patch("agent_loop.urllib.request.urlopen", return_value=mock_resp):
        with patch("agent_loop._http_get", side_effect=mock_http_get):
            with patch("agent_loop.time.sleep"):
                with patch("agent_loop.time.time", side_effect=mock_time):
                    with patch("agent_loop.STALL_SEC", 2):
                        from agent_loop import chat_via_bridge

                        out = chat_via_bridge("任务", new_chat=True)
                        assert out
                        assert "正在搜索" in out or "任务完成" in out
                        assert len(poll_calls) >= 1, f"应至少轮询 1 次, poll_calls={poll_calls}"

    print("✅ 第 3 轮：轮询卡死 → 检测后强制返回，未无限等待")


if __name__ == "__main__":
    print("=" * 50)
    print("3 轮模拟测试：验证完全避免卡死")
    print("=" * 50)
    test_round1_direct_completion()
    test_round2_poll_then_completion()
    test_round3_stall_then_return()
    print("\n" + "=" * 50)
    print("✅ 3 轮模拟测试全部通过，无卡死")
    print("=" * 50)
