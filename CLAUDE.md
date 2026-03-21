# Photo to Obsidian

将课本/书籍照片中的文字和图片信息提取为 Obsidian 笔记的 Python CLI 工具。

## 项目结构

- `photo_to_obsidian.py` — 主程序（图片编码、Claude API 提取、Obsidian 保存、交互问答）
- `requirements.txt` — Python 依赖
- `setup.sh` — 安装脚本

## 技术栈

- Python 3.10+
- Anthropic Claude API (`claude-sonnet-4-6`)
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
