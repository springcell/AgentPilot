"""
模拟测试：轮询逻辑与 ✅ 任务完成 识别
不依赖真实 API，纯逻辑验证
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 导入待测函数
from agent_loop import _is_intermediate

def test_is_intermediate():
    """验证 _is_intermediate 对各类文本的判断"""
    # 中间状态 → True
    assert _is_intermediate("") is True
    assert _is_intermediate("   ") is True
    assert _is_intermediate("正在搜索...") is True
    assert _is_intermediate("正在思考中") is True
    assert _is_intermediate("ChatGPT 说： 正在搜索\"A股\"") is True
    assert _is_intermediate("Searching for...") is True

    # 任务完成 → False（关键修复）
    done = "✅ 任务完成：已将今日A股10条最火新闻整理并保存到桌面\"新闻目录\"。"
    assert _is_intermediate(done) is False, f"任务完成应判为非中间: {done[:50]}"
    # fallback：无 emoji 时 "任务完成：" 也能识别
    done2 = "任务完成：已将新浪网首页第一条新闻保存到桌面文本文件。"
    assert _is_intermediate(done2) is False, f"任务完成 fallback 应判为非中间"

    # 正常回复（含 JSON）→ False
    assert _is_intermediate('{"command":"file_op","action":"list"}') is False
    assert _is_intermediate("```json\n{}\n```") is False

    # 纯文本回复（无中间词）→ False
    assert _is_intermediate("这是最终回复内容，已处理完毕。") is False

    print("✅ _is_intermediate 测试通过")


def test_poll_exit_conditions():
    """模拟 chat_via_bridge 轮询的退出条件（不实际发请求）"""
    for current in [
        "✅ 任务完成：已将今日A股10条最火新闻整理并保存到桌面\"新闻目录\"。",
        "任务完成：已将新浪网首页第一条新闻保存到桌面文本文件。",
    ]:
        assert ("✅ 任务完成" in current or "任务完成：" in current), current
        assert not _is_intermediate(current), current
    print("✅ 任务完成 退出条件验证通过")


if __name__ == "__main__":
    test_is_intermediate()
    test_poll_exit_conditions()
    print("\n✅ 全部模拟测试通过")
