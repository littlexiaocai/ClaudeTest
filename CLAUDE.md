# 心理学课程学习工具集

课程内容提取和学习辅助工具。

## 项目结构

- `photo_to_obsidian.py` — 照片提取工具（图片编码、Claude API 提取、Obsidian 保存、交互问答）
- `video_to_slides.py` — 视频 PPT 提取工具（CLI 入口、PDF 生成）
- `slide_detector.py` — 幻灯片检测模块（帧提取、相似度比较、切换检测、去重）
- `extract_transcript.py` — 语音逐字稿提取（Whisper 本地模型，输出 .txt + .srt）
- `record_psychology_videos.py` — 简单心理课程视频录制（Playwright + ffmpeg）
- `lecture_to_notes.py` — PPT + 逐字稿合并笔记（按时间戳匹配，输出 PDF，适合 NotebookLM）
- `requirements.txt` — Python 依赖
- `setup.sh` — 安装脚本

## 技术栈

- Python 3.10+
- Anthropic Claude API (`claude-sonnet-4-6`)
- OpenCV（视频帧提取和图像比较）
- scikit-image（SSIM 结构相似度）
- Pillow（图像处理）
- ReportLab（PDF 生成，图文混排）
- faster-whisper（语音转录，CTranslate2 加速）
- Playwright（浏览器自动化录制）
- Markdown + YAML frontmatter (Obsidian 兼容)

## 开发规范

- 代码和注释使用中文
- 遵循 PEP 8 风格
- 使用 type hints

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills:
`/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`,
`/design-consultation`, `/review`, `/ship`, `/browse`, `/qa`, `/qa-only`,
`/design-review`, `/setup-browser-cookies`, `/retro`, `/investigate`,
`/document-release`, `/codex`, `/careful`, `/freeze`, `/guard`, `/unfreeze`,
`/gstack-upgrade`

If gstack skills aren't working, run `cd .claude/skills/gstack && ./setup` to rebuild.
