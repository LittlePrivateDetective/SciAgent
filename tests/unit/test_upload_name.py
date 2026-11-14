"""上传文件名解码：浏览器发 UTF-8、Starlette 按 latin-1 解，中文名会变成乱码。

这三条断言对应三种真实来源：现代浏览器（UTF-8）、Windows 下的旧工具（GBK）、
以及本来就正常的名字（ASCII / 已经是 Unicode）。
"""
from __future__ import annotations

from app.api.routes import _decode_filename


def _mojibake(text: str, enc: str) -> str:
    """模拟 Starlette 的解码方式：把原始字节按 latin-1 读成字符串。"""
    return text.encode(enc).decode("latin-1")


def test_utf8_browser_filename():
    raw = _mojibake("计算机学报2023_ACRank_检索公理.pdf", "utf-8")
    assert raw != "计算机学报2023_ACRank_检索公理.pdf"
    assert _decode_filename(raw) == "计算机学报2023_ACRank_检索公理.pdf"


def test_gbk_filename_from_windows_tools():
    raw = _mojibake("新闻推荐增量学习.pdf", "gbk")
    assert _decode_filename(raw) == "新闻推荐增量学习.pdf"


def test_plain_ascii_untouched():
    assert _decode_filename("paper_2023.pdf") == "paper_2023.pdf"
    assert _decode_filename("") == ""


def test_already_unicode_untouched():
    # 已经是正确 Unicode 的名字不能因为 encode('latin-1') 失败被改写
    assert _decode_filename("论文.pdf") == "论文.pdf"


def test_latin1_name_not_mangled_into_chinese():
    """Müller 这类名字按 GBK 也能"解"出字符，但解不出两个以上汉字，必须原样保留。"""
    assert _decode_filename("Müller_LN_2016.pdf") == "Müller_LN_2016.pdf"
