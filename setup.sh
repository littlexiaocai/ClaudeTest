#!/bin/bash
# Photo to Obsidian - 安装脚本 (macOS)

set -e

echo "=== Photo to Obsidian 安装脚本 ==="
echo ""

# 检查 Python 3
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 Python 3，请先安装 Python 3"
    echo "  brew install python3"
    exit 1
fi

echo "Python 版本: $(python3 --version)"

# 创建虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi

echo "激活虚拟环境..."
source .venv/bin/activate

echo "安装依赖..."
pip install -r requirements.txt

echo ""
echo "=== 安装完成！==="
echo ""
echo "使用前请设置 API Key:"
echo "  export ANTHROPIC_API_KEY='your-api-key'"
echo ""
echo "快速开始:"
echo "  source .venv/bin/activate"
echo "  python photo_to_obsidian.py 照片.jpg --vault ~/ObsidianVault/心理学笔记"
echo ""
