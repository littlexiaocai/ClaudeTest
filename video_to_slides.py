#!/usr/bin/env python3
"""
Video to Slides — 从录播课程视频中提取 PPT 幻灯片并保存为 PDF

功能：
1. 从视频中检测幻灯片切换（区分动画呈现和真正换页）
2. 提取每张幻灯片的最完整版本（动画结束后的状态）
3. 自动去重（讲师来回翻页时只保留一份）
4. 导出为 PDF 文件（一个视频一个 PDF）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from slide_detector import (
    DetectionConfig,
    SlideSegment,
    deduplicate_slides,
    detect_slides,
    get_video_info,
    load_watermark_template,
)


def frames_to_pdf(
    slides: list[SlideSegment],
    output_path: str | Path,
    quality: int = 90,
) -> str:
    """
    将幻灯片帧列表保存为 PDF 文件。

    Args:
        slides: 幻灯片段列表
        output_path: 输出 PDF 路径
        quality: JPEG 压缩质量 (1-100)
    Returns:
        保存的 PDF 文件路径
    """
    if not slides:
        raise ValueError("没有幻灯片可以保存")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # OpenCV BGR -> RGB -> PIL Image
    images: list[Image.Image] = []
    for slide in slides:
        rgb = cv2.cvtColor(slide.best_frame, cv2.COLOR_BGR2RGB)
        images.append(Image.fromarray(rgb))

    # Pillow 多页 PDF
    images[0].save(
        str(output_path),
        "PDF",
        save_all=True,
        append_images=images[1:],
        quality=quality,
    )

    return str(output_path)


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


def process_video(
    video_path: str,
    output_path: str | None = None,
    config: DetectionConfig | None = None,
    deduplicate: bool = True,
    quality: int = 90,
    preview: bool = False,
    debug: bool = False,
    watermark_source: str | None = None,
) -> str:
    """
    处理单个视频：检测幻灯片 -> 去重 -> 保存 PDF。

    Returns:
        输出 PDF 文件路径
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")

    # 默认输出路径：与视频同名.pdf
    if output_path is None:
        output_path = str(video.with_suffix(".pdf"))

    # 获取视频信息
    info = get_video_info(video_path)
    duration_str = _format_time(info["duration_sec"])
    print(f"📹 视频信息: {video.name}")
    print(f"   分辨率: {info['width']}x{info['height']}, "
          f"帧率: {info['fps']:.1f}fps, "
          f"时长: {duration_str}")
    print()

    # 加载水印模板（如果提供）
    watermark_tmpl = None
    if watermark_source:
        print(f"🔖 加载水印模板: {watermark_source}")
        watermark_tmpl = load_watermark_template(watermark_source)

    # 检测幻灯片
    print("🔍 正在检测幻灯片...")
    slides = detect_slides(video_path, config, progress_callback=_print_progress,
                           watermark_template=watermark_tmpl)
    print()  # 换行（进度条后）
    print(f"   检测到 {len(slides)} 张幻灯片")

    if not slides:
        print("⚠️  未检测到任何幻灯片，请尝试调整 --stability-threshold 参数")
        sys.exit(1)

    # 去重
    if deduplicate:
        original_count = len(slides)
        slides = deduplicate_slides(slides)
        removed = original_count - len(slides)
        if removed > 0:
            print(f"   去重: 移除 {removed} 张重复幻灯片，剩余 {len(slides)} 张")

    # 打印幻灯片时间轴
    print()
    print("📋 幻灯片列表:")
    for i, slide in enumerate(slides):
        time_str = _format_time(slide.timestamp_sec)
        print(f"   [{i + 1:3d}] {time_str}")

    # 调试模式：保存每张幻灯片为单独图片
    if debug:
        debug_dir = Path(output_path).parent / f"{video.stem}_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        for i, slide in enumerate(slides):
            debug_path = debug_dir / f"slide_{i + 1:03d}_{_format_time(slide.timestamp_sec).replace(':', '-')}.jpg"
            cv2.imwrite(str(debug_path), slide.best_frame)
        print(f"\n🐛 调试图片已保存到: {debug_dir}/")

    # 预览模式：等待用户确认
    if preview:
        print()
        answer = input("是否生成 PDF？(y/n): ").strip().lower()
        if answer not in ("y", "yes", "是"):
            print("已取消")
            sys.exit(0)

    # 生成 PDF
    print()
    print("📄 正在生成 PDF...")
    result_path = frames_to_pdf(slides, output_path, quality)
    pdf_size_mb = Path(result_path).stat().st_size / (1024 * 1024)
    print(f"✅ 完成! 共 {len(slides)} 张幻灯片")
    print(f"   输出: {result_path} ({pdf_size_mb:.1f}MB)")

    return result_path


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="Video to Slides — 从录播课程视频中提取 PPT 幻灯片为 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python video_to_slides.py lecture.mp4                    # 基本用法
  python video_to_slides.py lecture.mp4 -o 第一课.pdf       # 指定输出
  python video_to_slides.py lecture.mp4 --preview          # 预览后再生成
  python video_to_slides.py lecture.mp4 --debug            # 调试模式
  python video_to_slides.py lecture.mp4 --fps 1            # 降低采样率加速
  python video_to_slides.py lecture.mp4 --stability-threshold 0.90  # 降低稳定判定阈值
        """,
    )

    parser.add_argument(
        "videos",
        nargs="+",
        help="视频文件路径（支持 MP4/MKV/AVI/MOV）",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出 PDF 路径（单个视频时）或输出目录（多个视频时）",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=2.0,
        help="采样帧率（默认: 2.0，越高越精确但越慢）",
    )
    parser.add_argument(
        "--stability-threshold",
        type=float,
        default=0.95,
        help="画面稳定判定阈值（默认: 0.95，越高越严格）",
    )
    parser.add_argument(
        "--min-stable-frames",
        type=int,
        default=3,
        help="最少连续稳定帧数（默认: 3，过滤短暂静止）",
    )
    parser.add_argument(
        "--watermark", "-w",
        help="包含水印的 PPT 截图路径（用于过滤非 PPT 画面）",
    )
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="启用幻灯片去重（默认不去重）",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=90,
        help="PDF 图像质量 1-100（默认: 90）",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="检测后预览幻灯片列表，确认后再生成 PDF",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="调试模式：保存所有检测到的帧为单独图片",
    )

    args = parser.parse_args()

    # 构建检测配置
    config = DetectionConfig(
        sample_fps=args.fps,
        stability_threshold=args.stability_threshold,
        min_stable_frames=args.min_stable_frames,
    )

    # 处理每个视频
    for i, video_path in enumerate(args.videos):
        if len(args.videos) > 1:
            print(f"\n{'='*60}")
            print(f"  处理视频 [{i + 1}/{len(args.videos)}]: {video_path}")
            print(f"{'='*60}\n")

        # 确定输出路径
        output = args.output
        if output and len(args.videos) > 1:
            # 多个视频时，output 作为目录
            out_dir = Path(output)
            out_dir.mkdir(parents=True, exist_ok=True)
            output = str(out_dir / (Path(video_path).stem + ".pdf"))

        process_video(
            video_path=video_path,
            output_path=output,
            config=config,
            deduplicate=args.dedup,
            quality=args.quality,
            preview=args.preview,
            debug=args.debug,
            watermark_source=args.watermark,
        )


if __name__ == "__main__":
    main()
