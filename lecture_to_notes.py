#!/usr/bin/env python3
"""
Lecture to Notes — 将视频课程的 PPT 幻灯片与逐字稿合并为 PDF 笔记

一步完成：
1. 从视频中提取 PPT 幻灯片（使用 slide_detector）
2. 从视频中转录语音逐字稿（使用 Whisper，或读取已有 SRT 文件）
3. 按时间戳匹配：每张 PPT 后面附上对应时段的讲解文字
4. 输出 PDF 文件（PPT 原图 + 逐字稿文字），适合上传 NotebookLM
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    PageBreak,
)

from slide_detector import (
    DetectionConfig,
    SlideSegment,
    deduplicate_slides,
    detect_slides,
    filter_low_content_slides,
    get_video_info,
    load_watermark_template,
)


# ---------------------------------------------------------------------------
# 中文字体注册
# ---------------------------------------------------------------------------

def _register_chinese_font() -> str:
    """注册中文字体，返回字体名称。按优先级搜索系统字体。"""
    candidates = [
        # macOS
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        # Linux
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        # Windows
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            font_name = "ChineseFont"
            pdfmetrics.registerFont(TTFont(font_name, path))
            return font_name

    # 找不到中文字体，使用默认字体（中文可能显示为方框）
    print("⚠️  未找到中文字体，PDF 中的中文可能无法正确显示")
    return "Helvetica"


# ---------------------------------------------------------------------------
# SRT 解析
# ---------------------------------------------------------------------------

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

    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            index = int(lines[0].strip())
        except ValueError:
            continue
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


# ---------------------------------------------------------------------------
# 自动转录（Whisper）
# ---------------------------------------------------------------------------

def _transcribe_video(
    video_path: str | Path,
    model_name: str = "medium",
    language: str = "zh",
) -> list[dict]:
    """
    使用 mlx-whisper 转录视频，返回 segments 列表。

    每个 segment 包含 start, end, text 字段。
    同时在视频同目录保存 .srt 和 .txt 文件供后续使用。
    """
    try:
        import mlx_whisper
    except ImportError:
        print("错误: 自动转录需要安装 mlx-whisper")
        print("  pip install mlx-whisper")
        sys.exit(1)

    import shutil

    if not shutil.which("ffmpeg"):
        print("错误: 未找到 ffmpeg，请先安装")
        sys.exit(1)

    from extract_transcript import (
        MLX_MODEL_MAP,
        extract_audio,
        save_srt,
        save_txt,
    )

    video = Path(video_path)
    model_repo = MLX_MODEL_MAP.get(model_name, model_name)

    print(f"🎤 使用 mlx-whisper 模型: {model_repo}")

    # 提取音频到临时文件
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(tmp_fd)
    tmp_audio = Path(tmp_path)

    try:
        print("   提取音频...")
        extract_audio(video, tmp_audio)

        print("   转录中（Apple Silicon GPU 加速）...")
        t1 = time.time()
        result = mlx_whisper.transcribe(
            str(tmp_audio),
            path_or_hf_repo=model_repo,
            language=language,
        )
        segments = result.get("segments", [])

        elapsed = time.time() - t1
        text_len = sum(len(seg["text"].strip()) for seg in segments)
        print(f"   转录完成 ({elapsed:.1f}秒, {text_len}字)")

        # 保存 SRT 和 TXT 供后续使用
        srt_path = video.with_suffix(".srt")
        txt_path = video.with_suffix(".txt")
        save_srt(segments, srt_path)
        save_txt(segments, txt_path)
        print(f"   → {srt_path.name}")
        print(f"   → {txt_path.name}")

        return segments
    finally:
        if tmp_audio.exists():
            tmp_audio.unlink()


# ---------------------------------------------------------------------------
# 段落合并
# ---------------------------------------------------------------------------

def _segments_to_paragraphs(
    segments: list[dict] | list[SrtSegment],
    pause_threshold: float = 1.0,
) -> list[dict]:
    """
    将逐字稿片段按停顿合并为自然段落。

    返回段落列表，每个段落包含 text, start, end。
    """
    if not segments:
        return []

    # 统一接口：SrtSegment 或 Whisper dict
    def _get(seg, key):
        if isinstance(seg, SrtSegment):
            return getattr(seg, {"start": "start_sec", "end": "end_sec", "text": "text"}[key])
        return seg[key]

    paragraphs: list[dict] = []
    current_texts: list[str] = []
    current_start = _get(segments[0], "start")
    current_end = _get(segments[0], "end")

    for i, seg in enumerate(segments):
        text = _get(seg, "text").strip()
        if not text:
            continue

        if i > 0 and _get(seg, "start") - current_end >= pause_threshold:
            if current_texts:
                paragraphs.append({
                    "text": "".join(current_texts),
                    "start": current_start,
                    "end": current_end,
                })
            current_texts = [text]
            current_start = _get(seg, "start")
        else:
            current_texts.append(text)

        current_end = _get(seg, "end")

    if current_texts:
        paragraphs.append({
            "text": "".join(current_texts),
            "start": current_start,
            "end": current_end,
        })

    return paragraphs


# ---------------------------------------------------------------------------
# 幻灯片与逐字稿匹配
# ---------------------------------------------------------------------------

def match_transcript_to_slides(
    slides: list[SlideSegment],
    paragraphs: list[dict],
) -> list[tuple[SlideSegment, str]]:
    """
    将段落化的逐字稿按时间戳匹配到对应的幻灯片。

    每个段落只归属一张幻灯片（按段落开始时间判定）。
    段落之间用空行分隔。
    """
    if not slides:
        return []

    # 构建每张幻灯片的时间范围
    slide_ranges: list[tuple[float, float]] = []
    for i, slide in enumerate(slides):
        start = slide.timestamp_sec
        end = slides[i + 1].timestamp_sec if i + 1 < len(slides) else float("inf")
        slide_ranges.append((start, end))

    # 每张幻灯片对应的文本列表
    slide_texts: list[list[str]] = [[] for _ in slides]

    # 每个段落按开始时间归属到唯一一张幻灯片
    for para in paragraphs:
        for i, (start, end) in enumerate(slide_ranges):
            if start <= para["start"] < end:
                slide_texts[i].append(para["text"])
                break

    results: list[tuple[SlideSegment, str]] = []
    for slide, texts in zip(slides, slide_texts):
        results.append((slide, "\n\n".join(texts)))

    return results


# ---------------------------------------------------------------------------
# PDF 生成
# ---------------------------------------------------------------------------

def _frame_to_rl_image(frame: np.ndarray, max_width: float) -> RLImage:
    """将 OpenCV 帧转换为 reportlab Image 对象。"""
    # BGR -> RGB -> PIL -> bytes
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)

    # 按最大宽度等比缩放
    w, h = pil_img.size
    ratio = max_width / w
    display_w = max_width
    display_h = h * ratio

    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=90)
    buf.seek(0)

    return RLImage(buf, width=display_w, height=display_h)


def _format_time(seconds: float) -> str:
    """将秒数格式化为 HH:MM:SS"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def generate_pdf(
    slides: list[SlideSegment],
    transcript_text: str,
    output_path: str,
    title: str = "",
    subtitle: str = "",
) -> None:
    """生成 PDF 文件：先放所有 PPT 图片，再放完整自然分段的逐字稿。"""
    font_name = _register_chinese_font()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
    )

    page_width = A4[0] - 30 * mm
    max_img_width = page_width

    style_title = ParagraphStyle(
        "SlideTitle",
        fontName=font_name,
        fontSize=14,
        leading=20,
        spaceAfter=6,
        textColor="#333333",
    )
    style_body = ParagraphStyle(
        "SlideBody",
        fontName=font_name,
        fontSize=10,
        leading=16,
        spaceAfter=8,
        textColor="#444444",
    )
    style_header = ParagraphStyle(
        "Header",
        fontName=font_name,
        fontSize=18,
        leading=24,
        spaceAfter=8,
        textColor="#222222",
    )
    style_sub = ParagraphStyle(
        "SubHeader",
        fontName=font_name,
        fontSize=10,
        leading=14,
        spaceAfter=12,
        textColor="#888888",
    )

    elements = []

    # 封面信息
    if title:
        elements.append(Paragraph(title, style_header))
    if subtitle:
        elements.append(Paragraph(subtitle, style_sub))
    if title or subtitle:
        elements.append(Spacer(1, 10 * mm))

    # --- 所有 PPT 图片 ---
    for i, slide in enumerate(slides):
        if i > 0:
            elements.append(PageBreak())

        time_str = _format_time(slide.timestamp_sec)
        elements.append(Paragraph(f"第 {i + 1} 页 [{time_str}]", style_title))
        elements.append(Spacer(1, 3 * mm))

        rl_img = _frame_to_rl_image(slide.best_frame, max_img_width)
        elements.append(rl_img)

    # --- 完整逐字稿（自然分段） ---
    if transcript_text.strip():
        elements.append(PageBreak())
        elements.append(Paragraph("讲解内容", style_header))
        elements.append(Spacer(1, 5 * mm))

        for para in transcript_text.split("\n\n"):
            para = para.strip()
            if para:
                safe = (para
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"))
                elements.append(Paragraph(safe, style_body))

    doc.build(elements)


# ---------------------------------------------------------------------------
# 进度条
# ---------------------------------------------------------------------------

def _print_progress(current: int, total: int) -> None:
    """打印进度条"""
    if total <= 0:
        return
    pct = min(100, int(current / total * 100))
    bar_len = 40
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  检测进度: [{bar}] {pct}%", end="", flush=True)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def generate_notes(
    video_path: str,
    srt_path: str | None = None,
    output_path: str | None = None,
    config: DetectionConfig | None = None,
    deduplicate: bool = True,
    watermark_source: str | None = None,
    whisper_model: str = "medium",
    whisper_language: str = "zh",
    pause_threshold: float = 1.0,
    debug_watermark: bool = False,
) -> str:
    """
    从视频生成结构化 PDF 笔记（PPT 原图 + 逐字稿）。

    如果提供 srt_path 则读取已有字幕，否则自动调用 Whisper 转录。

    Returns:
        输出 PDF 文件路径
    """
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(f"找不到视频文件: {video_path}")

    # 默认输出 PDF
    if output_path is None:
        output_path = str(video.with_suffix(".pdf"))

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 图片输出目录（备份用）
    img_dir = output.parent / f"{output.stem}_slides"
    img_dir.mkdir(parents=True, exist_ok=True)

    # 获取视频信息
    info = get_video_info(video_path)
    duration_str = _format_time(info["duration_sec"])
    print(f"📹 视频: {video.name} ({duration_str})")

    # --- 逐字稿 ---
    # 优先读取已有的 .txt 文件（已正确分段），其次读 SRT 重新合并，最后自动转录
    transcript_text = ""
    auto_txt = video.with_suffix(".txt")

    if srt_path:
        # 用户指定了 SRT 文件
        srt = Path(srt_path)
        if not srt.exists():
            raise FileNotFoundError(f"找不到字幕文件: {srt_path}")
        # 尝试读取同目录的 .txt 文件
        txt_for_srt = srt.with_suffix(".txt")
        if txt_for_srt.exists():
            print(f"📝 读取逐字稿: {txt_for_srt.name}")
            transcript_text = txt_for_srt.read_text(encoding="utf-8")
        else:
            print(f"📝 读取字幕: {srt.name}（重新合并段落）")
            srt_segments = parse_srt(srt_path)
            paragraphs = _segments_to_paragraphs(srt_segments, pause_threshold)
            transcript_text = "\n\n".join(p["text"] for p in paragraphs)
    elif auto_txt.exists():
        # 已有同名 .txt 文件，直接读取
        print(f"📝 读取逐字稿: {auto_txt.name}")
        transcript_text = auto_txt.read_text(encoding="utf-8")
    else:
        # 自动转录
        auto_srt = video.with_suffix(".srt")
        if auto_srt.exists():
            print(f"📝 发现已有字幕: {auto_srt.name}（重新合并段落）")
            srt_segments = parse_srt(str(auto_srt))
            paragraphs = _segments_to_paragraphs(srt_segments, pause_threshold)
            transcript_text = "\n\n".join(p["text"] for p in paragraphs)
        else:
            print("📝 未找到字幕文件，开始自动转录...")
            _transcribe_video(video_path, whisper_model, whisper_language)
            # 转录后会生成 .txt 文件，直接读取
            transcript_text = auto_txt.read_text(encoding="utf-8")

    para_count = len([p for p in transcript_text.split("\n\n") if p.strip()])
    print(f"   逐字稿 {len(transcript_text)} 字，{para_count} 个段落")

    # --- 幻灯片检测 ---
    watermark_tmpl = None
    if watermark_source:
        print(f"🔖 加载水印模板: {watermark_source}")
        watermark_tmpl = load_watermark_template(watermark_source)

    print("🔍 正在检测幻灯片...")
    slides = detect_slides(
        video_path, config,
        progress_callback=_print_progress,
        watermark_template=watermark_tmpl,
        debug_watermark=debug_watermark,
    )
    print()
    print(f"   检测到 {len(slides)} 张幻灯片")

    if not slides:
        print("⚠️  未检测到任何幻灯片")
        sys.exit(1)

    if deduplicate:
        original_count = len(slides)
        slides = deduplicate_slides(slides)
        removed = original_count - len(slides)
        if removed > 0:
            print(f"   去重: 移除 {removed} 张重复，剩余 {len(slides)} 张")

    # 过滤低内容页（品牌片头/片尾）
    before_filter = len(slides)
    slides = filter_low_content_slides(slides)
    filtered = before_filter - len(slides)
    if filtered > 0:
        print(f"   过滤: 移除 {filtered} 张低内容页，剩余 {len(slides)} 张")

    if not slides:
        print("⚠️  过滤后无剩余幻灯片")
        sys.exit(1)

    # --- 保存幻灯片图片（备份） ---
    print("🖼️  保存幻灯片图片...")
    for i, slide in enumerate(slides):
        img_path = img_dir / f"slide_{i + 1:03d}.jpg"
        cv2.imwrite(str(img_path), slide.best_frame)

    # --- 生成 PDF ---
    print("📄 生成 PDF...")
    generate_pdf(
        slides,
        transcript_text,
        str(output),
        title=video.stem,
        subtitle=f"来源: {video.name} | 时长: {duration_str} | 幻灯片: {len(slides)} 张",
    )

    print(f"\n✅ 完成! 共 {len(slides)} 张幻灯片")
    print(f"   PDF:  {output}")
    print(f"   图片: {img_dir}/")

    return str(output)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="Lecture to Notes — 从视频生成 PDF 笔记（PPT + 逐字稿）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python lecture_to_notes.py lecture.mp4 -w ppt.jpg --dedup           # 一步完成
  python lecture_to_notes.py lecture.mp4 --srt lecture.srt --dedup     # 使用已有字幕
  python lecture_to_notes.py lecture.mp4 -w ppt.jpg --model large     # 用更大模型转录
  python lecture_to_notes.py lecture.mp4 -w ppt.jpg -o 笔记.pdf       # 指定输出文件
        """,
    )

    parser.add_argument("video", help="视频文件路径")
    parser.add_argument(
        "--srt",
        help="SRT 字幕文件路径（不提供则自动转录）",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出 PDF 文件路径（默认: 与视频同名.pdf）",
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
        default=2.0,
        help="段落分段的停顿阈值（秒，默认: 2.0）",
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
        "--watermark-threshold", "-wt",
        type=float,
        default=None,
        help="水印匹配阈值（默认: 0.55，跨视频模板可能需要更低值）",
    )
    parser.add_argument(
        "--debug-watermark",
        action="store_true",
        help="输出水印检测调试信息（显示每帧的匹配分数）",
    )

    args = parser.parse_args()

    config_kwargs = dict(
        sample_fps=args.fps,
        stability_threshold=args.stability_threshold,
        min_stable_frames=args.min_stable_frames,
    )
    if args.watermark_threshold is not None:
        config_kwargs["watermark_match_threshold"] = args.watermark_threshold
    config = DetectionConfig(**config_kwargs)

    generate_notes(
        video_path=args.video,
        srt_path=args.srt,
        output_path=args.output,
        config=config,
        deduplicate=args.dedup,
        watermark_source=args.watermark,
        whisper_model=args.model,
        whisper_language=args.language,
        pause_threshold=args.pause_threshold,
        debug_watermark=args.debug_watermark,
    )


if __name__ == "__main__":
    main()
