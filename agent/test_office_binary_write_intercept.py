import sys

sys.path.insert(0, r"d:\AgentPilot\agent")

from agent_loop import _build_office_binary_write_correction


def main() -> None:
    xlsx = _build_office_binary_write_correction(
        r"C:\Users\admin\Desktop\推荐购买股票.xlsx",
        ".xlsx",
    )
    assert "openpyxl" in xlsx
    assert ".csv" in xlsx

    docx = _build_office_binary_write_correction(
        r"C:\Users\admin\Desktop\report.docx",
        ".docx",
    )
    assert "python-docx" in docx
    assert ".txt/.md" in docx

    pptx = _build_office_binary_write_correction(
        r"C:\Users\admin\Desktop\deck.pptx",
        ".pptx",
    )
    assert "python-pptx" in pptx

    print("ok")


if __name__ == "__main__":
    main()
