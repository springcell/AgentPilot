import sys

sys.path.insert(0, r"d:\AgentPilot\agent")

from json_parser import extract_json_blocks


def main() -> None:
    sample = (
        '{\n'
        '"command": "file_op",\n'
        '"action": "write",\n'
        '"path": "C:\\Users\\admin\\Desktop\\推荐购买股票.xlsx",\n'
        '"content": "股票代码\\t股票名称\\n000004\\t招商银行"\n'
        '}\n\n'
        '由于文件操作工具无法使用，我无法直接将文件保存到您的桌面。'
    )
    blocks = extract_json_blocks(sample)
    assert len(blocks) == 1
    assert blocks[0]["command"] == "file_op"
    assert blocks[0]["action"] == "write"
    assert blocks[0]["path"].endswith("推荐购买股票.xlsx")

    sample_single_slash = sample.replace("\\\\", "\\")
    blocks = extract_json_blocks(sample_single_slash)
    assert len(blocks) == 1
    assert blocks[0]["path"].endswith("推荐购买股票.xlsx")

    print("ok")


if __name__ == "__main__":
    main()
