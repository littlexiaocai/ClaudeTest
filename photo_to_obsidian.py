#!/usr/bin/env python3
"""
Photo to Obsidian - 将照片中的文字和图片内容提取到 Obsidian 笔记中

功能：
1. 读取照片（支持 JPG/PNG/HEIC），通过 Claude API 识别文字和图片内容
2. 生成 Obsidian 兼容的 Markdown 笔记
3. 支持批量处理多张照片
4. 支持交互式对话，对提取内容进行提问
"""

import anthropic
import base64
import sys
import os
import re
import argparse
import mimetypes
from pathlib import Path
from datetime import datetime


def encode_image(image_path: str) -> tuple[str, str]:
    """读取图片并编码为 base64，返回 (base64_data, media_type)"""
    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"找不到图片文件: {image_path}")

    suffix = path.suffix.lower()
    media_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".heic": "image/heic",
    }

    media_type = media_type_map.get(suffix)
    if not media_type:
        raise ValueError(f"不支持的图片格式: {suffix}，支持的格式: {', '.join(media_type_map.keys())}")

    with open(path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    return image_data, media_type


def extract_from_photos(image_paths: list[str], topic: str = "") -> str:
    """
    使用 Claude API 从照片中提取文字和图片信息。

    Args:
        image_paths: 图片文件路径列表
        topic: 可选的主题/学科提示，帮助 AI 更好地理解内容
    Returns:
        提取的 Markdown 格式文本
    """
    client = anthropic.Anthropic()

    # 构建包含所有图片的消息内容
    content = []
    for i, img_path in enumerate(image_paths):
        image_data, media_type = encode_image(img_path)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_data,
            },
        })
        if len(image_paths) > 1:
            content.append({
                "type": "text",
                "text": f"（以上是第 {i + 1}/{len(image_paths)} 张照片）",
            })

    topic_hint = f"这些内容属于「{topic}」领域。" if topic else ""

    content.append({
        "type": "text",
        "text": f"""请仔细阅读以上照片，完成以下任务：

1. **完整提取文字**：将照片中所有可见的文字内容逐字提取出来，保持原文的段落结构和层次。
2. **描述图片/图表**：如果照片中包含图表、示意图、流程图等视觉元素，请详细描述其内容和含义。
3. **标注说明**：如果有手写批注、划线、高亮等标记，也请一并记录。

{topic_hint}

请用 Markdown 格式输出，要求：
- 使用合适的标题层级（##、###）组织内容
- 图表描述用引用块（> ）标注
- 关键术语用 **加粗** 标记
- 如果内容涉及定义、理论或重要概念，用 Obsidian 的高亮语法（==文字==）标注
- 保持内容的学术准确性，不要添加原文没有的内容""",
    })

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8096,
        messages=[{"role": "user", "content": content}],
    )

    return response.content[0].text


def save_to_obsidian(
    content: str,
    output_dir: str,
    title: str = "",
    tags: list[str] | None = None,
) -> str:
    """
    将提取的内容保存为 Obsidian 兼容的 Markdown 文件。

    Args:
        content: Markdown 格式的提取内容
        output_dir: Obsidian vault 中的输出目录
        title: 笔记标题
        tags: Obsidian 标签列表
    Returns:
        保存的文件路径
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_str = datetime.now().strftime("%Y-%m-%d")

    if not title:
        # 从内容的第一行提取标题
        first_line = content.strip().split("\n")[0]
        title = re.sub(r"^#+\s*", "", first_line)[:50]

    # 清理文件名中的非法字符
    safe_title = re.sub(r'[<>:"/\\|?*]', "_", title)
    filename = f"{safe_title}_{timestamp}.md"

    # 构建 Obsidian 前置元数据
    tag_list = tags or []
    frontmatter_tags = "\n".join(f"  - {t}" for t in tag_list)
    frontmatter = f"""---
title: "{title}"
date: {date_str}
source: photo-extraction
tags:
{frontmatter_tags}
---

"""

    file_path = output_path / filename
    file_path.write_text(frontmatter + content, encoding="utf-8")

    return str(file_path)


def interactive_qa(image_paths: list[str], extracted_content: str):
    """
    基于提取的内容进行交互式问答。

    用户可以对照片内容提问，AI 会结合图片和提取的文字来回答。
    """
    client = anthropic.Anthropic()

    # 构建包含图片的上下文
    image_content = []
    for img_path in image_paths:
        image_data, media_type = encode_image(img_path)
        image_content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": image_data,
            },
        })

    # 维护对话历史
    messages = [
        {
            "role": "user",
            "content": image_content + [{
                "type": "text",
                "text": f"我正在学习以下内容，这是从课本照片中提取的笔记：\n\n{extracted_content}\n\n请记住这些内容，我接下来会对你提问。请回复「好的，我已经了解了这些内容，请随时提问。」",
            }],
        },
        {
            "role": "assistant",
            "content": "好的，我已经了解了这些内容，请随时提问。",
        },
    ]

    print("\n" + "=" * 50)
    print("进入交互问答模式（输入 q 或 quit 退出）")
    print("=" * 50 + "\n")

    while True:
        try:
            question = input("你的问题 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not question or question.lower() in ("q", "quit", "exit"):
            print("退出问答模式。")
            break

        messages.append({"role": "user", "content": question})

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system="你是一位心理学领域的学习助手。请基于用户提供的课本内容来回答问题。回答要准确、通俗易懂，并适当举例说明。如果问题超出了提供的内容范围，请诚实说明。",
            messages=messages,
        )

        answer = response.content[0].text
        messages.append({"role": "assistant", "content": answer})

        print(f"\n{answer}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Photo to Obsidian - 将照片中的文字提取到 Obsidian 笔记",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 提取单张照片
  python photo_to_obsidian.py photo.jpg

  # 提取多张照片并指定主题
  python photo_to_obsidian.py page1.jpg page2.jpg --topic "发展心理学"

  # 指定输出到 Obsidian vault
  python photo_to_obsidian.py photo.jpg --vault ~/Documents/ObsidianVault/心理学笔记

  # 提取后进入问答模式
  python photo_to_obsidian.py photo.jpg --chat

  # 添加标签
  python photo_to_obsidian.py photo.jpg --tags 心理学 认知发展 期末复习
        """,
    )

    parser.add_argument(
        "images",
        nargs="+",
        help="要提取的照片文件路径（支持 JPG/PNG/HEIC）",
    )
    parser.add_argument(
        "--vault", "-v",
        default="./output",
        help="Obsidian vault 中的输出目录路径（默认: ./output）",
    )
    parser.add_argument(
        "--topic", "-t",
        default="",
        help="内容所属的主题/学科（如：发展心理学），帮助 AI 更好地理解",
    )
    parser.add_argument(
        "--tags",
        nargs="*",
        default=[],
        help="Obsidian 标签（如：心理学 期末复习）",
    )
    parser.add_argument(
        "--title",
        default="",
        help="笔记标题（默认从内容自动提取）",
    )
    parser.add_argument(
        "--chat", "-c",
        action="store_true",
        help="提取完成后进入交互问答模式",
    )
    parser.add_argument(
        "--print-only", "-p",
        action="store_true",
        help="仅打印提取内容到终端，不保存文件",
    )

    args = parser.parse_args()

    # 验证图片文件
    for img in args.images:
        if not os.path.exists(img):
            print(f"错误: 找不到文件 {img}", file=sys.stderr)
            sys.exit(1)

    # 检查 API Key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("错误: 请设置环境变量 ANTHROPIC_API_KEY", file=sys.stderr)
        print("  export ANTHROPIC_API_KEY='your-api-key-here'", file=sys.stderr)
        sys.exit(1)

    print(f"正在处理 {len(args.images)} 张照片...")
    if args.topic:
        print(f"主题: {args.topic}")

    # 提取内容
    content = extract_from_photos(args.images, topic=args.topic)
    print("提取完成！\n")

    if args.print_only:
        print(content)
    else:
        # 保存到 Obsidian
        tags = args.tags if args.tags else []
        if args.topic:
            tags.insert(0, args.topic)

        file_path = save_to_obsidian(
            content=content,
            output_dir=args.vault,
            title=args.title,
            tags=tags,
        )
        print(f"笔记已保存到: {file_path}")

    # 交互问答模式
    if args.chat:
        interactive_qa(args.images, content)


if __name__ == "__main__":
    main()
