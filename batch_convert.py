#!/usr/bin/env python3
"""
批量视频转换 — 将文件夹中的所有视频批量生成 PPT + 逐字稿 PDF

功能：
- 扫描指定文件夹中的所有视频文件
- 显示总体进度（已完成/总数）
- 支持 Ctrl+C 随时终止，未完成的文件不会留下残余
- 自动跳过已完成的视频（检测输出目录中是否存在 PDF）
- 下次启动自动从未完成的部分继续
"""

from __future__ import annotations

import argparse
import signal
import sys
import shutil
from pathlib import Path

from lecture_to_notes import generate_notes
from slide_detector import DetectionConfig

# 支持的视频格式
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm"}

# 中断标志
_interrupted = False


def _signal_handler(signum, frame):
    """处理 Ctrl+C 信号"""
    global _interrupted
    _interrupted = True
    print("\n\n⏸️  收到中断信号，当前视频处理完毕后停止...")
    print("   再按一次 Ctrl+C 立即终止（当前视频结果不保存）")
    # 第二次 Ctrl+C 恢复默认行为（立即终止）
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def scan_videos(input_dir: Path) -> list[Path]:
    """扫描目录中的视频文件，按文件名排序"""
    videos = [
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]
    videos.sort(key=lambda p: p.name)
    return videos


def is_completed(video: Path, output_dir: Path) -> bool:
    """检查视频是否已完成转换（输出目录中存在同名 PDF）"""
    pdf_path = output_dir / f"{video.stem}.pdf"
    return pdf_path.exists()


def batch_convert(
    input_dir: str,
    output_dir: str,
    watermark_source: str | None = None,
    deduplicate: bool = True,
    whisper_model: str = "medium",
    whisper_language: str = "zh",
    pause_threshold: float = 1.0,
    watermark_threshold: float | None = None,
    sample_fps: float = 2.0,
    stability_threshold: float = 0.95,
    min_stable_frames: int = 3,
) -> None:
    """批量转换视频"""
    global _interrupted

    in_dir = Path(input_dir)
    out_dir = Path(output_dir)

    if not in_dir.exists():
        print(f"错误: 输入目录不存在: {input_dir}")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    # 扫描视频
    all_videos = scan_videos(in_dir)
    if not all_videos:
        print(f"未找到视频文件（支持格式: {', '.join(VIDEO_EXTENSIONS)}）")
        sys.exit(0)

    # 区分已完成和待处理
    completed = [v for v in all_videos if is_completed(v, out_dir)]
    pending = [v for v in all_videos if not is_completed(v, out_dir)]

    print(f"📂 输入目录: {in_dir}")
    print(f"📁 输出目录: {out_dir}")
    print(f"🎬 共 {len(all_videos)} 个视频，已完成 {len(completed)} 个，待处理 {len(pending)} 个")
    print()

    if not pending:
        print("✅ 所有视频已转换完成！")
        return

    # 显示待处理列表
    print("待处理文件:")
    for i, v in enumerate(pending, 1):
        print(f"  {i}. {v.name}")
    print()

    # 注册中断信号
    signal.signal(signal.SIGINT, _signal_handler)

    # 构建检测参数
    config_kwargs: dict = dict(
        sample_fps=sample_fps,
        stability_threshold=stability_threshold,
        min_stable_frames=min_stable_frames,
    )
    if watermark_threshold is not None:
        config_kwargs["watermark_match_threshold"] = watermark_threshold
    config = DetectionConfig(**config_kwargs)

    # 逐个处理
    done_count = len(completed)
    total = len(all_videos)

    for i, video in enumerate(pending, 1):
        if _interrupted:
            print(f"\n⏹️  已中断。完成 {done_count}/{total}，剩余 {len(pending) - i + 1} 个待处理。")
            break

        print("=" * 60)
        print(f"[{done_count + 1}/{total}] 正在处理: {video.name}")
        print("=" * 60)

        # 使用临时目录，完成后再移动到正式目录，确保中断时不留残余
        video_out_dir = out_dir / video.stem
        tmp_out_dir = out_dir / f".tmp_{video.stem}"

        try:
            # 清理可能残留的临时目录
            if tmp_out_dir.exists():
                shutil.rmtree(tmp_out_dir)
            tmp_out_dir.mkdir(parents=True, exist_ok=True)

            # 在临时目录中生成
            generate_notes(
                video_path=str(video),
                output_dir=str(tmp_out_dir),
                config=config,
                deduplicate=deduplicate,
                watermark_source=watermark_source,
                whisper_model=whisper_model,
                whisper_language=whisper_language,
                pause_threshold=pause_threshold,
            )

            # 成功后，只保留 PDF 到输出目录，清理中间文件
            tmp_pdf = tmp_out_dir / f"{video.stem}.pdf"
            final_pdf = out_dir / f"{video.stem}.pdf"
            if tmp_pdf.exists():
                shutil.move(str(tmp_pdf), str(final_pdf))

            # 清理临时目录（中间文件：.txt, .srt, _slides/ 等）
            if tmp_out_dir.exists():
                shutil.rmtree(tmp_out_dir)

            done_count += 1
            remaining = total - done_count
            print(f"\n✅ [{done_count}/{total}] 完成！剩余 {remaining} 个\n")

        except Exception as e:
            print(f"\n❌ 处理失败: {video.name}")
            print(f"   错误: {e}\n")
            # 清理临时目录
            if tmp_out_dir.exists():
                shutil.rmtree(tmp_out_dir)
            continue

    # 最终统计
    print("=" * 60)
    final_completed = [v for v in all_videos if is_completed(v, out_dir)]
    final_pending = [v for v in all_videos if not is_completed(v, out_dir)]
    print(f"📊 最终统计: {len(final_completed)}/{total} 完成")
    if final_pending:
        print(f"   未完成 {len(final_pending)} 个，下次运行将自动继续")
    else:
        print("🎉 全部完成！")


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="批量视频转换 — 将文件夹中所有视频生成 PPT + 逐字稿 PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python batch_convert.py ./videos ./output -w ppt.jpg --dedup
  python batch_convert.py ./videos ./output -w ppt.jpg --dedup -wt 0.55
  # 中断后重新运行，自动跳过已完成的视频:
  python batch_convert.py ./videos ./output -w ppt.jpg --dedup
        """,
    )

    parser.add_argument("input_dir", help="包含视频文件的输入目录")
    parser.add_argument("output_dir", help="输出目录（PDF、逐字稿、图片）")
    parser.add_argument(
        "--watermark", "-w",
        help="包含水印的 PPT 截图路径",
    )
    parser.add_argument(
        "--dedup",
        action="store_true",
        help="启用幻灯片去重",
    )
    parser.add_argument(
        "--model", "-m",
        default="medium",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Whisper 模型大小（默认: medium）",
    )
    parser.add_argument(
        "--language", "-l",
        default="zh",
        help="语言代码（默认: zh）",
    )
    parser.add_argument(
        "--pause-threshold", "-p",
        type=float,
        default=1.0,
        help="段落分段的停顿阈值（秒，默认: 1.0）",
    )
    parser.add_argument(
        "--watermark-threshold", "-wt",
        type=float,
        default=None,
        help="水印匹配阈值（默认: 0.55）",
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

    args = parser.parse_args()

    batch_convert(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        watermark_source=args.watermark,
        deduplicate=args.dedup,
        whisper_model=args.model,
        whisper_language=args.language,
        pause_threshold=args.pause_threshold,
        watermark_threshold=args.watermark_threshold,
        sample_fps=args.fps,
        stability_threshold=args.stability_threshold,
        min_stable_frames=args.min_stable_frames,
    )


if __name__ == "__main__":
    main()
