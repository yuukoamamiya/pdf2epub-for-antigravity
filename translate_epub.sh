#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 自动定位 ADC 凭证
if [ -z "$GOOGLE_APPLICATION_CREDENTIALS" ] && [ -f "$SCRIPT_DIR/vertex_adc.json" ]; then
    export GOOGLE_APPLICATION_CREDENTIALS="$SCRIPT_DIR/vertex_adc.json"
fi

# 自动检测执行环境 (uv / .venv / 系统路径)
if command -v uv &>/dev/null; then
    PDF2EPUB_CMD="uv run pdf2epub"
elif [ -f "$SCRIPT_DIR/.venv/Scripts/pdf2epub.exe" ]; then
    PDF2EPUB_CMD="$SCRIPT_DIR/.venv/Scripts/pdf2epub.exe"
elif [ -f "$SCRIPT_DIR/.venv/bin/pdf2epub" ]; then
    PDF2EPUB_CMD="$SCRIPT_DIR/.venv/bin/pdf2epub"
else
    PDF2EPUB_CMD="pdf2epub"
fi

CONFIG="${1:-config_epub.yaml}"

# 1. 从 config 提取书名
BOOK_TITLE=$(grep -E '^[[:space:]]*title:' "$CONFIG" | sed 's/^[[:space:]]*title:[[:space:]]*//' | sed 's/[[:space:]]*$//' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
if [ -z "$BOOK_TITLE" ]; then
    echo "❌ 错误：无法在 $CONFIG 中找到 title 字段！"
    exit 1
fi

# 2. 从 config 提取 EPUB 文件名
EPUB=$(grep -E '^[[:space:]]*input_epub:' "$CONFIG" | sed 's/^[[:space:]]*input_epub:[[:space:]]*//' | sed 's/[[:space:]]*$//' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")
if [ -z "$EPUB" ]; then
    echo "❌ 错误：无法在 $CONFIG 中找到 input_epub 字段，请检查配置文件顶部是否添加了该字段！"
    exit 1
fi

# 优先检查当前路径，其次检查 input/ 目录
if [ ! -f "$EPUB" ] && [ -f "input/$EPUB" ]; then
    EPUB="input/$EPUB"
fi

# 打印读取到的信息，让你一目了然
echo "======================================"
echo "📖 提取书名: $BOOK_TITLE"
echo "📄 提取文件: $EPUB"
echo "======================================"

# 3. 动态生成日志路径并创建文件夹
LOG="output/${BOOK_TITLE}/logs/epub.log"
mkdir -p "$(dirname "$LOG")"

echo "Resuming translation: $(date)" | tee "$LOG"

# 4. 执行命令
$PDF2EPUB_CMD -c "$CONFIG" translate-html -i "$EPUB" --resume 2>&1 | tee -a "$LOG"

echo "Building EPUB: $(date)" | tee -a "$LOG"
$PDF2EPUB_CMD -c "$CONFIG" build-html-epub 2>&1 | tee -a "$LOG"

echo "Done: $(date)" | tee -a "$LOG"
