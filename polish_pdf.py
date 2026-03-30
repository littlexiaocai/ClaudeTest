#!/usr/bin/env python3
"""
PDF 文字稿校对工具 — 基于现有 PDF，用 Claude API 加标点 + 修正同音错字

流程：
1. 从 PDF 中提取 PPT 图片和文字稿
2. 文字稿发给 Claude API 校对（加标点 + 改同音错字）
3. 用原始 PPT 图片 + 校对后文字稿重新生成 PDF
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from pathlib import Path

import anthropic
from PIL import Image
from pypdf import PdfReader
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


# ---------------------------------------------------------------------------
# 中文字体注册（复用 lecture_to_notes.py 的逻辑）
# ---------------------------------------------------------------------------

def _register_chinese_font() -> str:
    """注册中文字体，返回字体名称。"""
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            font_name = "ChineseFont"
            pdfmetrics.registerFont(TTFont(font_name, path))
            return font_name
    print("⚠️  未找到中文字体，PDF 中的中文可能无法正确显示")
    return "Helvetica"


# ---------------------------------------------------------------------------
# PDF 解析：提取图片和文字
# ---------------------------------------------------------------------------

def extract_from_pdf(pdf_path: str) -> tuple[list[dict], str, str, str]:
    """
    从 lecture_to_notes 生成的 PDF 中提取内容。

    Returns:
        (slides, transcript, title, subtitle)
        - slides: [{"label": "第 1 页 [02:15]", "image_bytes": bytes}, ...]
        - transcript: 逐字稿原文
        - title: 标题
        - subtitle: 副标题
    """
    reader = PdfReader(pdf_path)
    slides: list[dict] = []
    transcript_parts: list[str] = []
    title = ""
    subtitle = ""
    in_transcript = False

    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""

        # 提取图片
        images = []
        if hasattr(page, "images"):
            for img in page.images:
                images.append(img.data)

        # 判断页面类型
        lines = text.strip().split("\n")
        first_line = lines[0].strip() if lines else ""

        if page_idx == 0 and not first_line.startswith("第"):
            # 封面页：提取标题和副标题
            if lines:
                title = lines[0].strip()
            if len(lines) > 1:
                subtitle = lines[1].strip()
            # 封面可能也包含第一张 PPT
            if re.match(r"第\s*\d+\s*页", text):
                # 封面和第一张 PPT 在同一页
                slide_match = re.search(r"(第\s*\d+\s*页\s*\[[\d:]+\])", text)
                if slide_match and images:
                    slides.append({
                        "label": slide_match.group(1),
                        "image_bytes": images[0],
                    })
        elif re.match(r"第\s*\d+\s*页", first_line):
            # PPT 页
            label = first_line
            if images:
                slides.append({
                    "label": label,
                    "image_bytes": images[0],
                })
            in_transcript = False
        elif "讲解内容" in first_line or in_transcript:
            # 逐字稿页
            in_transcript = True
            content = text
            if "讲解内容" in first_line:
                # 去掉"讲解内容"标题行
                content = "\n".join(lines[1:])
            transcript_parts.append(content.strip())

    transcript = "\n\n".join(p for p in transcript_parts if p)
    return slides, transcript, title, subtitle


# ---------------------------------------------------------------------------
# Claude API 校对
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是心理学课程文字稿的校对专家。请对以下语音转录的中文文字稿进行校对：

1. 添加合适的标点符号（，。？！：；、""）
2. 修正语音识别产生的同音字/近音字错误（如心理学术语的误识别）
3. 不改变原文的表达方式、语序和语义
4. 不删除任何内容，不添加新的内容
5. 保持原有的段落结构（段落之间用空行分隔）

请直接输出校对后的文本，不要添加任何说明、解释或前缀。"""


def polish_text(
    text: str,
    batch_size: int = 2000,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 3,
) -> str:
    """
    调用 Claude API 校对文字稿。

    按段落分批处理，每批不超过 batch_size 字。
    网络超时自动重试。
    """
    import time as _time

    client = anthropic.Anthropic(timeout=120.0)

    # 按段落分割
    paragraphs = text.split("\n\n")
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > batch_size and current_batch:
            batches.append(current_batch)
            current_batch = [para]
            current_len = para_len
        else:
            current_batch.append(para)
            current_len += para_len

    if current_batch:
        batches.append(current_batch)

    if not batches:
        return text

    print(f"📝 文字稿共 {len(text)} 字，分 {len(batches)} 批校对")

    polished_parts: list[str] = []
    total_done = 0

    for i, batch in enumerate(batches, 1):
        batch_text = "\n\n".join(batch)
        batch_chars = len(batch_text)
        print(f"   [{i}/{len(batches)}] 校对中（{batch_chars} 字）...", end="", flush=True)

        # 带重试的 API 调用
        result = None
        for attempt in range(1, max_retries + 1):
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=8096,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": batch_text}],
                )
                result = response.content[0].text.strip()
                break
            except (anthropic.APITimeoutError, anthropic.APIConnectionError) as e:
                if attempt < max_retries:
                    wait = 2 ** attempt
                    print(f" 超时，{wait}秒后重试...", end="", flush=True)
                    _time.sleep(wait)
                else:
                    print(f" 失败")
                    raise RuntimeError(f"第 {i} 批校对失败（重试 {max_retries} 次后仍超时）: {e}")

        polished_parts.append(result)
        total_done += batch_chars
        print(f" 完成")

    return "\n\n".join(polished_parts)


# ---------------------------------------------------------------------------
# PDF 重新生成
# ---------------------------------------------------------------------------

def regenerate_pdf(
    slides: list[dict],
    transcript: str,
    output_path: str,
    title: str = "",
    subtitle: str = "",
) -> None:
    """用原始 PPT 图片 + 校对后的文字稿重新生成 PDF。"""
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

    style_title = ParagraphStyle(
        "SlideTitle", fontName=font_name, fontSize=14,
        leading=20, spaceAfter=6, textColor="#333333",
    )
    style_body = ParagraphStyle(
        "SlideBody", fontName=font_name, fontSize=10,
        leading=16, spaceAfter=8, textColor="#444444",
    )
    style_header = ParagraphStyle(
        "Header", fontName=font_name, fontSize=18,
        leading=24, spaceAfter=8, textColor="#222222",
    )
    style_sub = ParagraphStyle(
        "SubHeader", fontName=font_name, fontSize=10,
        leading=14, spaceAfter=12, textColor="#888888",
    )

    elements = []

    # 封面
    if title:
        elements.append(Paragraph(title, style_header))
    if subtitle:
        elements.append(Paragraph(subtitle, style_sub))
    if title or subtitle:
        elements.append(Spacer(1, 10 * mm))

    # PPT 图片页
    for i, slide in enumerate(slides):
        if i > 0:
            elements.append(PageBreak())

        elements.append(Paragraph(slide["label"], style_title))
        elements.append(Spacer(1, 3 * mm))

        # 将图片字节转为 reportlab Image
        img_buf = io.BytesIO(slide["image_bytes"])
        pil_img = Image.open(img_buf)
        w, h = pil_img.size
        ratio = page_width / w
        display_w = page_width
        display_h = h * ratio

        img_buf2 = io.BytesIO()
        pil_img.save(img_buf2, format="JPEG", quality=90)
        img_buf2.seek(0)
        elements.append(RLImage(img_buf2, width=display_w, height=display_h))

    # 校对后的逐字稿
    if transcript.strip():
        elements.append(PageBreak())
        elements.append(Paragraph("讲解内容", style_header))
        elements.append(Spacer(1, 5 * mm))

        for para in transcript.split("\n\n"):
            para = para.strip()
            if para:
                safe = (para
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"))
                elements.append(Paragraph(safe, style_body))

    doc.build(elements)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def polish_pdf(
    input_path: str,
    output_path: str | None = None,
    model: str = "claude-sonnet-4-6",
) -> str:
    """
    校对 PDF 中的文字稿。

    Args:
        input_path: 输入 PDF 文件路径
        output_path: 输出 PDF 路径（默认: 输入文件名_校对.pdf）
        model: Claude 模型名称

    Returns:
        输出文件路径
    """
    in_file = Path(input_path)
    if not in_file.exists():
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)

    if output_path is None:
        output_path = str(in_file.with_stem(f"{in_file.stem}_校对"))

    print(f"📄 读取 PDF: {in_file.name}")
    slides, transcript, title, subtitle = extract_from_pdf(input_path)
    print(f"   PPT: {len(slides)} 张，文字稿: {len(transcript)} 字")

    if not transcript.strip():
        print("⚠️  未找到文字稿内容")
        sys.exit(1)

    # 调用 Claude API 校对
    print("🔤 开始校对...")
    polished = polish_text(transcript, model=model)

    # 重新生成 PDF
    print("📄 生成校对后的 PDF...")
    regenerate_pdf(slides, polished, output_path, title, subtitle)

    print(f"\n✅ 完成!")
    print(f"   输出: {output_path}")
    return output_path


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="PDF 文字稿校对 — 加标点 + 修正同音错字",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  python polish_pdf.py 课程笔记.pdf                    # 输出 课程笔记_校对.pdf
  python polish_pdf.py 课程笔记.pdf -o 校对后.pdf       # 指定输出
        """,
    )

    parser.add_argument("input", help="输入 PDF 文件路径")
    parser.add_argument(
        "--output", "-o",
        help="输出 PDF 文件路径（默认: 输入文件名_校对.pdf）",
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-sonnet-4-6",
        help="Claude 模型（默认: claude-sonnet-4-6）",
    )

    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("错误: 请设置环境变量 ANTHROPIC_API_KEY")
        print("  export ANTHROPIC_API_KEY='your-api-key-here'")
        sys.exit(1)

    polish_pdf(args.input, args.output, args.model)


if __name__ == "__main__":
    main()
