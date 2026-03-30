#!/usr/bin/env python3
"""
PDF 文字稿校对工具 — 基于现有 PDF，用 Claude API 加标点 + 修正同音错字

流程：
1. 从 PDF 中提取 PPT 图片和文字稿（保持一一对应关系）
2. 文字稿并发发给 Claude API 校对（加标点 + 改同音错字）
3. 用原始 PPT 图片 + 校对后文字稿重新生成 PDF（保持原始结构）
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# 中文字体注册
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
# PDF 解析：提取 PPT + 对应文字稿（保持一一对应）
# ---------------------------------------------------------------------------

def extract_from_pdf(pdf_path: str) -> tuple[list[dict], str, str]:
    """
    从 lecture_to_notes 生成的 PDF 中提取内容，保持 PPT 和文字稿的对应关系。

    Returns:
        (slide_sections, title, subtitle)
        - slide_sections: [{"label": "第 1 页 [02:15]", "image_bytes": bytes, "transcript": "文字稿..."}, ...]
        - title: 标题
        - subtitle: 副标题
    """
    reader = PdfReader(pdf_path)
    slide_sections: list[dict] = []
    title = ""
    subtitle = ""
    in_transcript = False
    transcript_parts: list[str] = []

    for page_idx, page in enumerate(reader.pages):
        text = page.extract_text() or ""

        # 提取图片
        images = []
        if hasattr(page, "images"):
            for img in page.images:
                images.append(img.data)

        # 在整页文本中搜索 PPT 标记（不只看第一行，防止 pypdf 提取顺序不同）
        slide_match = re.search(r"(第\s*\d+\s*页\s*\[[\d:]+\])", text)
        has_transcript_header = "讲解内容" in text
        lines = text.strip().split("\n")

        if slide_match and images:
            # PPT 页 — 先保存上一张的文字稿
            if slide_sections and transcript_parts:
                slide_sections[-1]["transcript"] = "\n\n".join(
                    p for p in transcript_parts if p
                )
                transcript_parts = []
            in_transcript = False

            # 封面页的标题和副标题（第一页且有 PPT 之前的文本）
            if page_idx == 0 and lines:
                first_line = lines[0].strip()
                if not re.match(r"第\s*\d+\s*页", first_line):
                    title = first_line
                    if len(lines) > 1 and not re.match(r"第\s*\d+\s*页", lines[1].strip()):
                        subtitle = lines[1].strip()

            slide_sections.append({
                "label": slide_match.group(1),
                "image_bytes": images[0],
                "transcript": "",
            })
        elif has_transcript_header or in_transcript:
            # 逐字稿页
            in_transcript = True
            content = text
            if has_transcript_header:
                content = "\n".join(l for l in lines if "讲解内容" not in l)
            if content.strip():
                transcript_parts.append(content.strip())
        elif page_idx == 0:
            # 纯封面页（无 PPT）
            if lines:
                title = lines[0].strip()
            if len(lines) > 1:
                subtitle = lines[1].strip()

    # 处理最后一张 PPT 的文字稿
    if slide_sections and transcript_parts:
        slide_sections[-1]["transcript"] = "\n\n".join(
            p for p in transcript_parts if p
        )

    # 如果所有文字稿都集中在最后（所有 PPT 之后），均分给各幻灯片
    slides_with_text = sum(1 for s in slide_sections if s["transcript"])
    if slides_with_text <= 1 and len(slide_sections) > 1:
        # 文字稿集中在最后一张，需要保持原样（所有 PPT 后跟全部文字稿）
        pass

    return slide_sections, title, subtitle


# ---------------------------------------------------------------------------
# Claude API 校对（并发）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是心理学课程文字稿的校对专家。请对以下语音转录的中文文字稿进行校对：

1. 添加合适的标点符号（，。？！：；、""）
2. 修正语音识别产生的同音字/近音字错误（如心理学术语的误识别）
3. 不改变原文的表达方式、语序和语义
4. 不删除任何内容，不添加新的内容
5. 保持原有的段落结构（段落之间用空行分隔）

请直接输出校对后的文本，不要添加任何说明、解释或前缀。"""


def _call_api(
    client: anthropic.Anthropic,
    batch_text: str,
    model: str,
    batch_idx: int,
    total_batches: int,
    max_retries: int = 3,
) -> tuple[int, str]:
    """单批次 API 调用（带重试），返回 (batch_idx, result)。"""
    for attempt in range(1, max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=8096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": batch_text}],
            )
            return batch_idx, response.content[0].text.strip()
        except (anthropic.APITimeoutError, anthropic.APIConnectionError):
            if attempt < max_retries:
                _time.sleep(2 ** attempt)
            else:
                raise RuntimeError(
                    f"第 {batch_idx + 1} 批校对失败（重试 {max_retries} 次后仍超时）"
                )
    return batch_idx, batch_text  # 不应到达


def polish_text(
    text: str,
    batch_size: int = 2000,
    model: str = "claude-sonnet-4-6",
    max_workers: int = 5,
) -> str:
    """
    调用 Claude API 并发校对文字稿。

    按段落分批处理，每批不超过 batch_size 字。
    使用 max_workers 个并发线程加速。
    """
    client = anthropic.Anthropic(timeout=120.0)

    # 按段落分割成批次
    paragraphs = text.split("\n\n")
    batches: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for para in paragraphs:
        para_len = len(para)
        if current_len + para_len > batch_size and current_parts:
            batches.append("\n\n".join(current_parts))
            current_parts = [para]
            current_len = para_len
        else:
            current_parts.append(para)
            current_len += para_len

    if current_parts:
        batches.append("\n\n".join(current_parts))

    if not batches:
        return text

    total = len(batches)
    print(f"📝 文字稿共 {len(text)} 字，分 {total} 批校对（{max_workers} 并发）")

    # 并发调用 API
    results: dict[int, str] = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_call_api, client, batch, model, i, total): i
            for i, batch in enumerate(batches)
        }

        for future in as_completed(futures):
            batch_idx, result = future.result()
            results[batch_idx] = result
            completed += 1
            print(f"   [{completed}/{total}] 完成第 {batch_idx + 1} 批")

    # 按原始顺序拼接
    return "\n\n".join(results[i] for i in range(total))


# ---------------------------------------------------------------------------
# PDF 重新生成（保持原始结构）
# ---------------------------------------------------------------------------

def regenerate_pdf(
    slide_sections: list[dict],
    output_path: str,
    title: str = "",
    subtitle: str = "",
) -> None:
    """用原始 PPT 图片 + 校对后的文字稿重新生成 PDF，保持一一对应。"""
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

    # 检查文字稿是否集中在最后一张（原始 PDF 是"所有 PPT 后跟全部文字稿"格式）
    slides_with_text = sum(1 for s in slide_sections if s.get("transcript", "").strip())
    all_text_at_end = (
        slides_with_text <= 1
        and len(slide_sections) > 1
        and slide_sections[-1].get("transcript", "").strip()
    )

    if all_text_at_end:
        # 原始格式：所有 PPT 图片，然后全部文字稿
        full_transcript = slide_sections[-1]["transcript"]

        for i, section in enumerate(slide_sections):
            if i > 0:
                elements.append(PageBreak())
            elements.append(Paragraph(section["label"], style_title))
            elements.append(Spacer(1, 3 * mm))
            elements.append(_image_to_rl(section["image_bytes"], page_width))

        if full_transcript.strip():
            elements.append(PageBreak())
            elements.append(Paragraph("讲解内容", style_header))
            elements.append(Spacer(1, 5 * mm))
            for para in full_transcript.split("\n\n"):
                para = para.strip()
                if para:
                    elements.append(Paragraph(_safe_xml(para), style_body))
    else:
        # 一一对应格式：每页 PPT 后跟对应文字稿
        for i, section in enumerate(slide_sections):
            if i > 0:
                elements.append(PageBreak())

            elements.append(Paragraph(section["label"], style_title))
            elements.append(Spacer(1, 3 * mm))
            elements.append(_image_to_rl(section["image_bytes"], page_width))

            transcript = section.get("transcript", "").strip()
            if transcript:
                elements.append(Spacer(1, 5 * mm))
                for para in transcript.split("\n\n"):
                    para = para.strip()
                    if para:
                        elements.append(Paragraph(_safe_xml(para), style_body))

    doc.build(elements)


def _image_to_rl(image_bytes: bytes, max_width: float) -> RLImage:
    """将图片字节转为 reportlab Image 对象。"""
    img_buf = io.BytesIO(image_bytes)
    pil_img = Image.open(img_buf)
    w, h = pil_img.size
    ratio = max_width / w
    display_w = max_width
    display_h = h * ratio

    out_buf = io.BytesIO()
    pil_img.save(out_buf, format="JPEG", quality=90)
    out_buf.seek(0)
    return RLImage(out_buf, width=display_w, height=display_h)


def _safe_xml(text: str) -> str:
    """转义 XML 特殊字符。"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def polish_pdf(
    input_path: str,
    output_path: str | None = None,
    model: str = "claude-sonnet-4-6",
    max_workers: int = 5,
) -> str:
    """校对 PDF 中的文字稿，保持原始 PPT + 文字稿对应结构。"""
    in_file = Path(input_path)
    if not in_file.exists():
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)

    if output_path is None:
        output_path = str(in_file.with_stem(f"{in_file.stem}_校对"))

    print(f"📄 读取 PDF: {in_file.name}")
    slide_sections, title, subtitle = extract_from_pdf(input_path)

    total_chars = sum(len(s.get("transcript", "")) for s in slide_sections)
    slides_with_text = sum(1 for s in slide_sections if s.get("transcript", "").strip())
    print(f"   PPT: {len(slide_sections)} 张，文字稿: {total_chars} 字（{slides_with_text} 段配对）")

    if total_chars == 0:
        print("⚠️  未找到文字稿内容")
        sys.exit(1)

    # 校对每张 PPT 对应的文字稿
    print("🔤 开始校对...")
    for i, section in enumerate(slide_sections):
        transcript = section.get("transcript", "").strip()
        if transcript:
            print(f"\n   --- {section['label']} ---")
            section["transcript"] = polish_text(
                transcript, model=model, max_workers=max_workers,
            )

    # 重新生成 PDF
    print("\n📄 生成校对后的 PDF...")
    regenerate_pdf(slide_sections, output_path, title, subtitle)

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
  python polish_pdf.py 课程笔记.pdf -m claude-haiku-4-5-20251001  # 用 Haiku 更便宜
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
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=5,
        help="并发数（默认: 5）",
    )

    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("错误: 请设置环境变量 ANTHROPIC_API_KEY")
        print("  export ANTHROPIC_API_KEY='your-api-key-here'")
        sys.exit(1)

    polish_pdf(args.input, args.output, args.model, args.workers)


if __name__ == "__main__":
    main()
