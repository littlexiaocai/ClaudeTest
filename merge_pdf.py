#!/usr/bin/env python3
"""
PDF 合并工具 — 将文件夹中的 PDF 按文件名编号顺序合并为一个 PDF

支持自然排序：2_xxx.pdf 排在 10_xxx.pdf 前面。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pypdf import PdfWriter


def natural_sort_key(path: Path) -> list:
    """自然排序：按文件名中的数字大小排序，而非字符串排序"""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r"(\d+)", path.name)]


def merge_pdfs(input_dir: str, output_path: str | None = None) -> str:
    """
    合并目录中的所有 PDF 文件。

    Args:
        input_dir: 包含 PDF 文件的目录
        output_path: 输出文件路径（默认: 目录下的 merged.pdf）

    Returns:
        输出文件路径
    """
    in_dir = Path(input_dir)
    if not in_dir.exists():
        print(f"错误: 目录不存在: {input_dir}")
        sys.exit(1)

    # 扫描 PDF 文件并自然排序
    pdf_files = sorted(
        [f for f in in_dir.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"],
        key=natural_sort_key,
    )

    if not pdf_files:
        print(f"未找到 PDF 文件: {input_dir}")
        sys.exit(0)

    # 默认输出路径
    if output_path is None:
        output_path = str(in_dir / "merged.pdf")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 显示合并顺序
    print(f"📂 输入目录: {in_dir}")
    print(f"📄 找到 {len(pdf_files)} 个 PDF 文件，合并顺序:")
    total_pages = 0
    writer = PdfWriter()

    for i, pdf in enumerate(pdf_files, 1):
        reader_pages = len(PdfWriter())  # 临时计数
        # 直接用 writer.append 合并
        page_count_before = len(writer.pages)
        writer.append(str(pdf))
        page_count = len(writer.pages) - page_count_before
        total_pages += page_count
        print(f"  {i:3d}. {pdf.name} ({page_count} 页)")

    # 写入输出文件
    with open(output, "wb") as f:
        writer.write(f)

    print(f"\n✅ 合并完成!")
    print(f"   共 {len(pdf_files)} 个文件，{total_pages} 页")
    print(f"   输出: {output}")

    return str(output)


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="PDF 合并工具 — 按文件名编号顺序合并目录中的 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python merge_pdf.py /path/to/pdf目录
  python merge_pdf.py /path/to/pdf目录 -o 课程合集.pdf
        """,
    )

    parser.add_argument("input_dir", help="包含 PDF 文件的目录")
    parser.add_argument(
        "--output", "-o",
        help="输出 PDF 文件路径（默认: 目录下的 merged.pdf）",
    )

    args = parser.parse_args()
    merge_pdfs(args.input_dir, args.output)


if __name__ == "__main__":
    main()
