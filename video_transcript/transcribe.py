"""
音频转写 + Markdown 生成脚本

使用 faster-whisper 对录制的音频进行语音识别，生成逐字稿。

模式:
  默认模式：带时间戳的逐行输出
  --smart-segment：Whisper 出原始文本，Haiku API 做语义分段（推荐用于课程转录）

用法:
    python transcribe.py --input output/01_精神分析概要.ogg
    python transcribe.py --input output/01_精神分析概要.ogg --smart-segment
    python transcribe.py --input output/01_精神分析概要.ogg --model large-v3
"""

import argparse
import os
import sys
from pathlib import Path


def format_timestamp(seconds: float) -> str:
    """将秒数转换为 [HH:MM:SS] 格式"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"[{h:02d}:{m:02d}:{s:02d}]"
    return f"[{m:02d}:{s:02d}]"


def transcribe_audio(audio_path: Path, model_size: str = "large-v3") -> list[dict]:
    """使用 faster-whisper 转写音频，返回带时间戳的段落列表"""
    from faster_whisper import WhisperModel

    print(f"加载 Whisper 模型: {model_size}")
    print("（首次运行会下载模型，约 3GB，请耐心等待）")

    # Apple Silicon 用 CPU + int8 效果不错且内存友好
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print(f"开始转写: {audio_path}")
    segments, info = model.transcribe(
        str(audio_path),
        language="zh",
        vad_filter=True,  # 过滤静音段
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    print(f"检测到语言: {info.language} (概率: {info.language_probability:.2f})")

    results = []
    for segment in segments:
        results.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })
        # 实时打印进度
        ts = format_timestamp(segment.start)
        print(f"  {ts} {segment.text.strip()}")

    return results


def generate_markdown(segments: list[dict], title: str = "课程转写") -> str:
    """将转写结果生成带时间戳的 Markdown 格式"""
    lines = [
        f"# {title}",
        "",
    ]

    if segments:
        total_duration = segments[-1]["end"]
        lines.append(f"**总时长**: {format_timestamp(total_duration)}")
        lines.append(f"**段落数**: {len(segments)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    for seg in segments:
        ts = format_timestamp(seg["start"])
        lines.append(f"{ts} {seg['text']}")
        lines.append("")

    return "\n".join(lines)


def segments_to_plain_text(segments: list[dict]) -> str:
    """将 Whisper 段落合并为纯文本（去掉时间戳）"""
    return "".join(seg["text"] for seg in segments)


def smart_segment(raw_text: str, title: str) -> str:
    """
    用 Claude Haiku 将转录文本按语义自然分段。

    输入：Whisper 输出的连续文本
    输出：分段后的 Markdown 文本
    """
    import anthropic

    client = anthropic.Anthropic()

    # 对长文本分块处理（Haiku 上下文限制）
    # 每块约 8000 字（中文），留足空间给 prompt 和输出
    chunk_size = 8000
    chunks = []
    for i in range(0, len(raw_text), chunk_size):
        chunks.append(raw_text[i:i + chunk_size])

    all_paragraphs = []

    for i, chunk in enumerate(chunks):
        if len(chunks) > 1:
            print(f"  语义分段: 处理第 {i + 1}/{len(chunks)} 块...")

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": f"""请将以下课程转录文本按语义自然分段。

要求：
1. 按内容主题和讲述逻辑自然分段
2. 每段之间空一行
3. 保留原文，不要修改、总结或删减任何内容
4. 修正明显的语音识别错误（如同音字错误）
5. 添加必要的标点符号
6. 不要添加标题、编号或任何额外标注

课程名称：{title}

转录文本：
{chunk}"""
            }]
        )

        all_paragraphs.append(response.content[0].text)

    # 组合所有分块结果
    segmented_text = "\n\n".join(all_paragraphs)

    # 生成最终 Markdown
    lines = [
        f"# {title}",
        "",
        "---",
        "",
        segmented_text,
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="音频转写 + Markdown 生成")
    parser.add_argument("--input", "-i", type=str, required=True, help="输入音频文件路径")
    parser.add_argument("--model", type=str, default="large-v3",
                        help="Whisper 模型大小 (tiny/base/small/medium/large-v3)")
    parser.add_argument("--title", type=str, default=None, help="Markdown 标题")
    parser.add_argument("--output", "-o", type=str, default=None, help="输出 Markdown 文件路径")
    parser.add_argument("--smart-segment", action="store_true",
                        help="使用 Haiku API 做语义分段（推荐用于课程转录）")
    args = parser.parse_args()

    audio_path = Path(args.input)
    if not audio_path.exists():
        print(f"错误: 文件不存在 {audio_path}")
        sys.exit(1)

    # 转写
    segments = transcribe_audio(audio_path, args.model)

    if not segments:
        print("警告: 未识别到任何语音内容")
        sys.exit(1)

    title = args.title or audio_path.stem

    if args.smart_segment:
        # 语义分段模式：Whisper 文本 → Haiku 分段
        raw_text = segments_to_plain_text(segments)
        print(f"\n开始语义分段（使用 Haiku API）...")
        markdown = smart_segment(raw_text, title)
    else:
        # 默认模式：带时间戳
        markdown = generate_markdown(segments, title)

    # 输出
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = audio_path.with_suffix(".md")

    output_path.write_text(markdown, encoding="utf-8")
    print(f"\n转写完成: {output_path}")
    print(f"   模式: {'语义分段' if args.smart_segment else '时间戳'}")
    print(f"   共 {len(segments)} 个 Whisper 段落")


if __name__ == "__main__":
    main()
