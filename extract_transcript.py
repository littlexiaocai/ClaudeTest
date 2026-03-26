#!/usr/bin/env python3
"""从课程视频中提取语音逐字稿。

使用 OpenAI Whisper 本地模型将视频中的中文语音转录为文字。
输出两种格式：
  - .txt  按语句自然分段的纯文本（适合喂给大模型）
  - .srt  带时间戳的标准字幕文件（适合回溯定位）
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import whisper
except ImportError:
    print("错误: 请安装 openai-whisper")
    print("  pip install openai-whisper")
    sys.exit(1)


def check_prerequisites():
    if not shutil.which("ffmpeg"):
        print("错误: 未找到 ffmpeg，请先安装:")
        print("  brew install ffmpeg")
        sys.exit(1)


def extract_audio(video_path: Path, audio_path: Path):
    """从视频中提取音频为 16kHz 单声道 WAV（Whisper 原生格式）。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(audio_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg 音频提取失败: {result.stderr[-500:]}")


def format_timestamp(seconds: float) -> str:
    """将秒数格式化为 SRT 时间戳 (HH:MM:SS,mmm)。"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_to_paragraphs(
    segments: list,
    pause_threshold: float = 0.8,
) -> list[dict]:
    """
    根据语音停顿将 Whisper segments 合并为自然段落。

    当相邻句子之间的停顿超过 pause_threshold 秒时，开始新段落。
    内容完全不丢失，只是重新分组。

    返回段落列表，每个段落包含:
      - text: 合并后的文本
      - start: 段落起始时间
      - end: 段落结束时间
    """
    if not segments:
        return []

    paragraphs: list[dict] = []
    current_texts: list[str] = []
    current_start = segments[0]["start"]
    current_end = segments[0]["end"]

    for i, seg in enumerate(segments):
        text = seg["text"].strip()
        if not text:
            continue

        if i > 0 and seg["start"] - current_end >= pause_threshold:
            # 停顿超过阈值，结束当前段落
            if current_texts:
                paragraphs.append({
                    "text": "".join(current_texts),
                    "start": current_start,
                    "end": current_end,
                })
            current_texts = [text]
            current_start = seg["start"]
        else:
            current_texts.append(text)

        current_end = seg["end"]

    # 最后一个段落
    if current_texts:
        paragraphs.append({
            "text": "".join(current_texts),
            "start": current_start,
            "end": current_end,
        })

    return paragraphs


def save_txt(segments: list, output_path: Path, pause_threshold: float = 0.8):
    """保存为按段落自然分段的纯文本。"""
    paragraphs = merge_to_paragraphs(segments, pause_threshold)
    texts = [p["text"] for p in paragraphs]
    output_path.write_text("\n\n".join(texts), encoding="utf-8")


def save_srt(segments: list, output_path: Path):
    """保存为标准 SRT 字幕格式。"""
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        start = format_timestamp(seg["start"])
        end = format_timestamp(seg["end"])
        text = seg["text"].strip()
        if text:
            srt_lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    output_path.write_text("\n".join(srt_lines), encoding="utf-8")


def process_videos(video_paths: list, model_name: str, language: str,
                   force: bool, output_dir=None, pause_threshold: float = 0.8):
    """批量处理视频文件。"""
    print(f"正在加载 Whisper 模型: {model_name} ...")
    t0 = time.time()
    model = whisper.load_model(model_name)
    print(f"模型加载完成 ({time.time() - t0:.1f}秒)\n")

    total = len(video_paths)
    done, skipped, failed = 0, 0, 0

    for i, video_path in enumerate(video_paths, 1):
        base_name = video_path.stem
        out_dir = output_dir or video_path.parent
        txt_path = out_dir / f"{base_name}.txt"
        srt_path = out_dir / f"{base_name}.srt"

        if txt_path.exists() and not force:
            print(f"[{i}/{total}] 跳过 (已存在): {video_path.name}")
            skipped += 1
            continue

        print(f"[{i}/{total}] 正在处理: {video_path.name}")
        tmp_audio = None
        try:
            t1 = time.time()

            # 提取音频到临时文件
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav")
            os.close(tmp_fd)
            tmp_audio = Path(tmp_path)

            print(f"         提取音频...")
            extract_audio(video_path, tmp_audio)

            # Whisper 转录
            print(f"         转录中...")
            result = model.transcribe(str(tmp_audio), language=language)
            segments = result.get("segments", [])

            # 保存两种格式
            save_txt(segments, txt_path, pause_threshold)
            save_srt(segments, srt_path)

            elapsed = time.time() - t1
            text_len = sum(len(seg["text"].strip()) for seg in segments)
            print(f"         完成 ({elapsed:.1f}秒, {text_len}字)")
            print(f"         → {txt_path.name}")
            print(f"         → {srt_path.name}")
            done += 1

        except Exception as e:
            print(f"         失败: {e}")
            failed += 1
        finally:
            if tmp_audio and tmp_audio.exists():
                tmp_audio.unlink()

    print(f"\n{'=' * 40}")
    print(f"转录完成: {done} 成功, {skipped} 跳过, {failed} 失败")
    print(f"{'=' * 40}")


def main():
    parser = argparse.ArgumentParser(
        description="从课程视频中提取语音逐字稿 (使用 Whisper 本地模型)",
        epilog="""示例:
  python extract_transcript.py ./videos/                    # 转录整个目录
  python extract_transcript.py ./videos/第01课.mp4           # 转录单个文件
  python extract_transcript.py ./videos/ --model large      # 用更大模型提高准确率
  python extract_transcript.py ./videos/ -o ./transcripts/  # 指定输出目录
  python extract_transcript.py ./videos/ --force            # 强制重新转录
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="视频文件或包含视频的目录")
    parser.add_argument("--model", "-m", default="medium",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper 模型大小 (默认: medium，中文效果好)")
    parser.add_argument("--language", "-l", default="zh",
                        help="语言代码 (默认: zh)")
    parser.add_argument("--force", "-f", action="store_true",
                        help="重新转录已存在的文件")
    parser.add_argument("--output-dir", "-o", default=None,
                        help="输出目录 (默认: 与视频同目录)")
    parser.add_argument("--pause-threshold", "-p", type=float, default=0.8,
                        help="段落分段的停顿阈值（秒，默认: 0.8）")

    args = parser.parse_args()

    check_prerequisites()

    input_path = Path(os.path.expanduser(args.input)).resolve()
    if input_path.is_file():
        video_paths = [input_path]
    elif input_path.is_dir():
        video_paths = sorted(input_path.glob("*.mp4"))
        if not video_paths:
            print(f"目录中未找到 MP4 文件: {input_path}")
            sys.exit(1)
        print(f"找到 {len(video_paths)} 个视频文件\n")
    else:
        print(f"错误: 路径不存在: {input_path}")
        sys.exit(1)

    output_dir = None
    if args.output_dir:
        output_dir = Path(os.path.expanduser(args.output_dir)).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    process_videos(video_paths, args.model, args.language, args.force, output_dir,
                   args.pause_threshold)


if __name__ == "__main__":
    main()
