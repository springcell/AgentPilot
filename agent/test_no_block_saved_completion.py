import sys

sys.path.insert(0, r"d:\AgentPilot\agent")

from loop_common import LoopRuntime, handle_no_block_task_complete


def _task_done_marker(text: str) -> bool:
    value = str(text or "")
    return "Task complete" in value or "任务完成" in value


def main() -> None:
    runtime = LoopRuntime("task1", "draw an icon", "default", [], False, "default")
    text = (
        "图标已经生成并保存在桌面上。\n\n"
        "[File saved to: C:\\Users\\admin\\Desktop\\chatgpt_generated_1773812681.png]\n"
        "✅ Task complete: File saved as chatgpt_generated_1773812681.png"
    )
    ok, feedback = handle_no_block_task_complete(runtime, text, _task_done_marker)
    assert ok
    assert "[File saved to:" in feedback
    assert runtime.had_intercepted_write is True
    print("ok")


if __name__ == "__main__":
    main()
