import sys

sys.path.insert(0, r"d:\AgentPilot\agent")

from loop_flows import (
    _WIN_SAVE_PATH_RE,
    _completed_direct_delivery_write_response,
    _build_direct_delivery_write_example,
    _direct_delivery_require_real_write,
    _extract_claimed_save_paths,
    _guess_direct_delivery_write_path,
    _task_suggests_save_to_disk,
)


def main() -> None:
    assert _WIN_SAVE_PATH_RE.search(r"C:\Users\admin\Desktop\report.txt")
    assert _WIN_SAVE_PATH_RE.search(r"C:/Users/admin/Desktop/report.txt")
    extracted = _extract_claimed_save_paths(
        "saved to C:\\Users\\admin\\Desktop\\my report.txt",
        r"C:\Users\admin\Desktop",
    )
    assert extracted == [r"C:\Users\admin\Desktop\my report.txt"]
    extracted = _extract_claimed_save_paths(
        r"saved to %DESKTOP%\my report.txt",
        r"C:\Users\admin\Desktop",
    )
    assert extracted == [r"C:\Users\admin\Desktop\my report.txt"]
    extracted = _extract_claimed_save_paths(
        r"saved to Desktop\my report.txt",
        r"C:\Users\admin\Desktop",
    )
    assert extracted == [r"C:\Users\admin\Desktop\my report.txt"]
    assert _guess_direct_delivery_write_path(
        "帮我推荐今天最值得购买的股票，放在桌面",
        '✅ Task complete: 已保存到 "推荐股票.xlsx"',
        r"C:\Users\admin\Desktop",
    ).endswith(".csv")
    example = _build_direct_delivery_write_example(
        "帮我推荐今天最值得购买的股票，放在桌面",
        '✅ Task complete: 已保存到 "推荐股票.xlsx"',
        r"C:\Users\admin\Desktop",
    )
    assert '"action":"write"' in example
    assert ".csv" in example

    assert _task_suggests_save_to_disk("Find 10 today news and put on desktop")
    assert _task_suggests_save_to_disk("保存到桌面 txt 文件")
    assert not _task_suggests_save_to_disk("Translate this JSON to Chinese and reply inline")
    assert not _task_suggests_save_to_disk("Explain this file format inline")

    gate = _direct_delivery_require_real_write(
        task_text="Find 10 today news and put on desktop",
        ai_text="✅ Task complete: saved to C:/Users/admin/Desktop/recommended_stocks_detailed.txt",
        wrote_this_session=False,
        desktop_hint=r"C:\Users\admin\Desktop",
    )
    assert gate and "file_op write" in gate

    gate = _direct_delivery_require_real_write(
        task_text="Return the answer directly, do not save a file",
        ai_text="✅ Task complete: Here is the answer inline.",
        wrote_this_session=False,
        desktop_hint=r"C:\Users\admin\Desktop",
    )
    assert gate is None

    gate = _direct_delivery_require_real_write(
        task_text="Find 10 today news and put on desktop",
        ai_text="✅ Task complete",
        wrote_this_session=True,
        desktop_hint=r"C:\Users\admin\Desktop",
        last_write_path=r"C:\Users\admin\Desktop\recommended_stocks_detailed.txt",
    )
    assert gate and "recommended_stocks_detailed.txt" in gate

    done = _completed_direct_delivery_write_response(
        [{"command": "file_op", "action": "write", "path": r"C:\Users\admin\Desktop\推荐股票.csv"}],
        [{"success": True, "stdout": "wrote file"}],
    )
    assert "[File saved to: C:\\Users\\admin\\Desktop\\推荐股票.csv]" in done
    assert "Task complete" in done

    print("ok")


if __name__ == "__main__":
    main()
