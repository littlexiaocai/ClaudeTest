#!/usr/bin/env python3
"""
提取指定说话人的内容 — 从多人对话文字稿中提取指定说话人的发言

格式要求：
  说话人1  00:08
  这是说话人1的内容...
  可以有多个段落...

  说话人2  05:30
  这是说话人2的内容...

用法:
  python extract_speaker.py input.txt                    # 默认提取说话人1
  python extract_speaker.py input.txt -s 说话人1         # 指定说话人
  python extract_speaker.py input.txt -o output.txt      # 指定输出文件
  python extract_speaker.py input.txt --keep-timestamps   # 保留时间戳行
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def extract_speaker(
    input_path: str,
    speaker: str = "说话人1",
    output_path: str | None = None,
    keep_timestamps: bool = False,
) -> str:
    """
    从多人对话文字稿中提取指定说话人的发言。

    Args:
        input_path: 输入文本文件路径
        speaker: 要提取的说话人名称（默认: 说话人1）
        output_path: 输出文件路径（默认: 输入文件名_说话人.txt）
        keep_timestamps: 是否保留时间戳行

    Returns:
        输出文件路径
    """
    in_file = Path(input_path)
    if not in_file.exists():
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)

    if output_path is None:
        output_path = str(in_file.with_stem(f"{in_file.stem}_{speaker}"))

    content = in_file.read_text(encoding="utf-8")
    lines = content.split("\n")

    # 说话人标记正则：说话人X  HH:MM 或 说话人X  HH:MM:SS
    speaker_pattern = re.compile(r"^(说话人\d+)\s+\d{1,2}:\d{2}")

    result_lines: list[str] = []
    current_speaker: str | None = None
    all_speakers: set[str] = set()

    for line in lines:
        match = speaker_pattern.match(line)
        if match:
            # 遇到新的说话人标记
            current_speaker = match.group(1)
            all_speakers.add(current_speaker)
            if current_speaker == speaker:
                result_lines.append(line)
        else:
            # 普通文本行，属于当前说话人
            if current_speaker == speaker:
                result_lines.append(line)

    # 清理：去掉首尾空行，合并连续空行
    text = "\n".join(result_lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    # 统计
    other_speakers = sorted(all_speakers - {speaker})
    total_chars = len(text.replace("\n", "").replace(" ", ""))
    para_count = len([p for p in text.split("\n\n") if p.strip()])

    print(f"📄 输入文件: {in_file.name}")
    print(f"👤 提取说话人: {speaker}")
    if other_speakers:
        print(f"   已删除: {', '.join(other_speakers)}")
    print(f"   提取内容: {total_chars} 字，{para_count} 个段落")

    # 写入输出
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(text, encoding="utf-8")
    print(f"   输出: {out_file}")

    return str(out_file)


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="提取指定说话人的内容 — 从多人对话文字稿中提取指定说话人的发言",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python extract_speaker.py 督导记录.txt                    # 默认提取说话人1（老师）
  python extract_speaker.py 督导记录.txt -s 说话人2         # 提取说话人2
  python extract_speaker.py 督导记录.txt -o 老师发言.txt     # 指定输出文件名
  python extract_speaker.py 督导记录.txt --keep-timestamps   # 保留时间戳
        """,
    )

    parser.add_argument("input", help="输入文本文件路径")
    parser.add_argument(
        "--speaker", "-s",
        default="说话人1",
        help="要提取的说话人名称（默认: 说话人1）",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出文件路径（默认: 输入文件名_说话人.txt）",
    )
    parser.add_argument(
        "--keep-timestamps",
        action="store_true",
        help="保留时间戳行",
    )

    args = parser.parse_args()
    extract_speaker(args.input, args.speaker, args.output, args.keep_timestamps)


if __name__ == "__main__":
    main()
