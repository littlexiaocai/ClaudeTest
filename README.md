# Photo to Obsidian - 照片文字提取工具

将课本/书籍照片中的文字和图片信息提取为 Obsidian 笔记，并支持交互式问答。

## 功能

- **文字提取**：通过 Claude AI 多模态能力，精准识别照片中的印刷文字和手写内容
- **图表理解**：自动描述照片中的图表、示意图、流程图等视觉内容
- **Obsidian 输出**：生成带前置元数据（frontmatter）的 Markdown 文件，直接放入 Obsidian vault
- **批量处理**：支持一次处理多张照片，自动合并为一篇笔记
- **交互问答**：提取后可进入对话模式，对内容提问（适合学习场景）

## 安装

### 前提条件

- macOS 系统
- Python 3.10+
- [Anthropic API Key](https://console.anthropic.com/)

### 一键安装

```bash
chmod +x setup.sh
./setup.sh
```

### 手动安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 设置 API Key

```bash
export ANTHROPIC_API_KEY='your-api-key-here'

# 推荐：加入 shell 配置文件以持久化
echo "export ANTHROPIC_API_KEY='your-api-key'" >> ~/.zshrc
```

## 使用方法

### 基本用法 - 提取单张照片

```bash
python photo_to_obsidian.py 照片.jpg
```

### 指定 Obsidian vault 输出目录

```bash
python photo_to_obsidian.py 照片.jpg --vault ~/Documents/ObsidianVault/心理学笔记
```

### 批量处理多张照片

```bash
python photo_to_obsidian.py page1.jpg page2.jpg page3.jpg --topic "发展心理学"
```

### 添加标签和标题

```bash
python photo_to_obsidian.py 照片.jpg \
  --title "皮亚杰认知发展理论" \
  --tags 心理学 认知发展 期末复习 \
  --vault ~/ObsidianVault/心理学
```

### 提取后进入问答模式

```bash
python photo_to_obsidian.py 照片.jpg --chat
```

在问答模式中，你可以直接提问，例如：
- "请解释这个理论的核心观点"
- "这个概念和上一章的 XX 有什么关系？"
- "帮我总结这页的要点"

### 仅打印到终端（不保存文件）

```bash
python photo_to_obsidian.py 照片.jpg --print-only
```

## 完整参数

| 参数 | 缩写 | 说明 | 默认值 |
|------|------|------|--------|
| `images` | | 照片文件路径（必填，支持多个） | - |
| `--vault` | `-v` | Obsidian vault 输出目录 | `./output` |
| `--topic` | `-t` | 主题/学科（如"发展心理学"） | 空 |
| `--tags` | | Obsidian 标签 | 空 |
| `--title` | | 笔记标题 | 自动提取 |
| `--chat` | `-c` | 提取后进入问答模式 | 否 |
| `--print-only` | `-p` | 仅打印，不保存文件 | 否 |

## 输出示例

生成的 Markdown 文件结构：

```markdown
---
title: "皮亚杰认知发展理论"
date: 2026-03-13
source: photo-extraction
tags:
  - 心理学
  - 认知发展
---

## 皮亚杰的认知发展阶段

**皮亚杰** (Jean Piaget) 提出了四个==认知发展阶段==：

### 1. 感知运动阶段（0-2岁）
...

> 📊 **图表描述**：页面中部的流程图展示了四个阶段的递进关系...
```

## 推荐工作流

1. 用手机拍下课本页面（建议光线充足、拍正）
2. 通过 AirDrop 传到 Mac
3. 运行命令提取到 Obsidian vault
4. 用 `--chat` 模式对内容提问加深理解
5. 在 Obsidian 中整理、链接笔记

## Mac 快捷方式（可选）

可以在 `~/.zshrc` 中添加别名简化操作：

```bash
alias photo2note='source ~/ClaudeTest/.venv/bin/activate && python ~/ClaudeTest/photo_to_obsidian.py'

# 使用方式：
# photo2note 照片.jpg --vault ~/ObsidianVault/心理学 --chat
```
