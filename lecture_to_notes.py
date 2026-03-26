#!/usr/bin/env python3
"""
Lecture to Notes — 将视频课程的 PPT 幻灯片与逐字稿合并为结构化笔记

功能：
1. 从视频中提取 PPT 幻灯片（使用 slide_detector）
2. 从 SRT 字幕文件中读取逐字稿（需先用 extract_transcript.py 生成）
3. 按时间戳匹配：每张 PPT 后面附上对应时段的讲解文字
4. 输出 Markdown 文件 + 幻灯片图片目录
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import cv2

from slide_detector import (
    DetectionConfig,
    SlideSegment,
    deduplicate_slides,
    detect_slides,
    get_video_info,
    load_watermark_template,
)


@dataclass
class SrtSegment:
    """SRT 字幕中的一个片段"""
    index: int
    start_sec: float
    end_sec: float
    text: str


def parse_srt(srt_path: str | Path) -> list[SrtSegment]:
    """解析 SRT 字幕文件，返回带时间戳的文本片段列表。"""
    content = Path(srt_path).read_text(encoding="utf-8")
    segments: list[SrtSegment] = []

    # SRT 格式: 序号\n开始 --> 结束\n文本\n
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue

        try:
            index = int(lines[0].strip())
        except ValueError:
            continue

        # 解析时间戳 HH:MM:SS,mmm --> HH:MM:SS,mmm
        time_match = re.match(
            r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})",
            lines[1].strip(),
        )
        if not time_match:
            continue

        g = [int(x) for x in time_match.groups()]
        start_sec = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        end_sec = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = "\n".join(lines[2:]).strip()

        if text:
            segments.append(SrtSegment(index, start_sec, end_sec, text))

    return segments


def match_transcript_to_slides(
    slides: list[SlideSegment],
    transcript: list[SrtSegment],
) -> list[tuple[SlideSegment, str]]:
    """
    将逐字稿片段按时间戳匹配到对应的幻灯片。

    每张幻灯片对应的时间范围：从该幻灯片出现到下一张幻灯片出现。
    最后一张幻灯片对应到视频结束。
    """
    if not slides:
        return []

    results: list[tuple[SlideSegment, str]] = []

    for i, slide in enumerate(slides):
        start_time = slide.timestamp_sec
        if i + 1 < len(slides):
            end_time = slides[i + 1].timestamp_sec
        else:
            # 最后一张幻灯片：取所有剩余文本
            end_time = float("inf")

        # 收集此时间范围内的逐字稿
        matched_texts: list[str] = []
        for seg in transcript:
            # 字幕片段与幻灯片时间范围有交集
            if seg.end_sec > start_time and seg.start_sec < end_time:
                matched_texts.append(seg.text)

        results.append((slide, "\n".join(matched_texts)))

    return results


def _format_time(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _print_progress(current: int, total: int) -> None:
    """打印进度条"""
    if total <= 0:
        return
    pct = min(100, int(current / total * 100))
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  检测进度: [{bar}] {pct}%", end="", flush=True)


def generate_notes(
    video_path: str,
    srt_path: str,
    output_path: str | None = None,
    config: DetectionConfig | None = None,
    deduplicate: bool = True,
    watermark_source: str | None = None,
) -> str:
    """
    从视频 + SRT 生成结构化笔记（Markdown + 图片）。

    Returns:
        输出 Markdown 文件路径
    """
    video = Path(video_path)
    srt = Path(srt_path)

    if not video.exists():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")
    if not srt.exists():
        raise FileNotFoundError(f"找不到字幕文件: {srt_path}")

    # 默认输出路径
    if output_path is None:
        output_path = str(video.with_suffix(".md"))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 图片输出目录
    img_dir = output.parent / f"{output.stem}_slides"
    img_dir.mkdir(parents=True, exist_ok=True)

    # 获取视频信息
    info = get_video_info(video_path)
    duration_str = _format_time(info["duration_sec"])
    print(f"📹 视频: {video.name} ({duration_str})")

    # 解析 SRT
    print(f"📝 解析字幕: {srt.name}")
    transcript = parse_srt(srt_path)
    print(f"   共 {len(transcript)} 个字幕片段")

    # 加载水印模板
    watermark_tmpl = None
    if watermark_source:
        print(f"🔖 加载水印模板: {watermark_source}")
        watermark_tmpl = load_watermark_template(watermark_source)

    # 检测幻灯片
    print("🔍 正在检测幻灯片...")
    slides = detect_slides(
        video_path, config,
        progress_callback=_print_progress,
        watermark_template=watermark_tmpl,
    )
    print()
    print(f"   检测到 {len(slides)} 张幻灯片")

    if not slides:
        print("⚠️  未检测到任何幻灯片")
        sys.exit(1)

    # 去重
    if deduplicate:
        original_count = len(slides)
        slides = deduplicate_slides(slides)
        removed = original_count - len(slides)
        if removed > 0:
            print(f"   去重: 移除 {removed} 张重复，剩余 {len(slides)} 张")

    # 匹配逐字稿
    print("🔗 匹配逐字稿到幻灯片...")
    matched = match_transcript_to_slides(slides, transcript)

    # 保存幻灯片图片
    print("🖼️  保存幻灯片图片...")
    slide_image_paths: list[str] = []
    for i, (slide, _) in enumerate(matched):
        img_name = f"slide_{i + 1:03d}.jpg"
        img_path = img_dir / img_name
        cv2.imwrite(str(img_path), slide.best_frame)
        slide_image_paths.append(img_name)

    # 生成 Markdown
    print("📄 生成 Markdown 笔记...")
    md_lines: list[str] = []
    md_lines.append(f"# {video.stem}\n")
    md_lines.append(f"> 来源视频: {video.name}  ")
    md_lines.append(f"> 时长: {duration_str}  ")
    md_lines.append(f"> 幻灯片数: {len(matched)}\n")

    for i, ((slide, text), img_name) in enumerate(zip(matched, slide_image_paths)):
        time_str = _format_time(slide.timestamp_sec)
        md_lines.append(f"---\n")
        md_lines.append(f"## 第 {i + 1} 页 [{time_str}]\n")
        # 图片使用相对路径
        md_lines.append(f"![幻灯片 {i + 1}]({img_dir.name}/{img_name})\n")
        if text.strip():
            md_lines.append(f"### 讲解内容\n")
            md_lines.append(f"{text}\n")
        else:
            md_lines.append(f"*（此页无讲解内容）*\n")

    md_content = "\n".join(md_lines)
    output.write_text(md_content, encoding="utf-8")

    print(f"\n✅ 完成! 共 {len(matched)} 张幻灯片")
    print(f"   笔记: {output}")
    print(f"   图片: {img_dir}/")

    return str(output)


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="Lecture to Notes — 合并 PPT 幻灯片与逐字稿为结构化笔记",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python lecture_to_notes.py lecture.mp4 lecture.srt                      # 基本用法
  python lecture_to_notes.py lecture.mp4 lecture.srt -o 第一课笔记.md      # 指定输出
  python lecture_to_notes.py lecture.mp4 lecture.srt -w ppt_sample.jpg    # 使用水印过滤
  python lecture_to_notes.py lecture.mp4 lecture.srt --dedup              # 去重
        """,
    )

    parser.add_argument("video", help="视频文件路径")
    parser.add_argument("srt", help="SRT 字幕文件路径（由 extract_transcript.py 生成）")
    parser.add_argument(
        "--output", "-o",
        help="输出 Markdown 文件路径（默认: 与视频同名.md）",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=2.0,
        help="采样帧率（默认: 2.0）",
    )
    parser.add_argument(
        "--stability-threshold",
        type=float,
        default=0.95,
        help="画面稳定判定阈值（默认: 0.95）",
    )
    parser.add_argument(
        "--min-stable-frames",
        type=int,
        default=3,
        help="最少连续稳定帧数（默认: 3）",
    )
    parser.add_argument(
        "--watermark", "-w",
        help="包含水印的 PPT 截图路径",
    )
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="启用幻灯片去重",
    )

    args = parser.parse_args()

    config = DetectionConfig(
        sample_fps=args.fps,
        stability_threshold=args.stability_threshold,
        min_stable_frames=args.min_stable_frames,
    )

    generate_notes(
        video_path=args.video,
        srt_path=args.srt,
        output_path=args.output,
        config=config,
        deduplicate=args.dedup,
        watermark_source=args.watermark,
    )


if __name__ == "__main__":
    main()
