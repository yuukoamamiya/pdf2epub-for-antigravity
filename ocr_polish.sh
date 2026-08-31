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
    PYTHON_CMD="uv run python"
elif [ -f "$SCRIPT_DIR/.venv/Scripts/pdf2epub.exe" ]; then
    PDF2EPUB_CMD="$SCRIPT_DIR/.venv/Scripts/pdf2epub.exe"
    PYTHON_CMD="$SCRIPT_DIR/.venv/Scripts/python.exe"
elif [ -f "$SCRIPT_DIR/.venv/bin/pdf2epub" ]; then
    PDF2EPUB_CMD="$SCRIPT_DIR/.venv/bin/pdf2epub"
    PYTHON_CMD="$SCRIPT_DIR/.venv/bin/python"
else
    PDF2EPUB_CMD="pdf2epub"
    PYTHON_CMD="python3"
fi

CONFIG="${1:-config.yaml}"

# 👇 1. 从 config 中提取书名 (剔除前后空格和引号)
BOOK_TITLE=$(grep -E '^[[:space:]]*title:' "$CONFIG" | sed 's/^[[:space:]]*title:[[:space:]]*//' | sed 's/[[:space:]]*$//' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")

if [ -z "$BOOK_TITLE" ]; then
    echo "❌ 错误：无法在 $CONFIG 中找到 title 字段，请检查文件！"
    exit 1
fi

# 👇 2. 从 config 中提取 PDF 文件名
PDF=$(grep -E '^[[:space:]]*input_pdf:' "$CONFIG" | sed 's/^[[:space:]]*input_pdf:[[:space:]]*//' | sed 's/[[:space:]]*$//' | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")

if [ -z "$PDF" ]; then
    echo "❌ 错误：无法在 $CONFIG 中找到 input_pdf 字段，请检查配置文件顶部是否添加了该字段！"
    exit 1
fi

# 优先检查当前路径，其次检查 input/ 目录
if [ ! -f "$PDF" ] && [ -f "input/$PDF" ]; then
    PDF="input/$PDF"
fi

# 👇 3. 动态拼接并创建日志文件夹
LOG_DIR="output/${BOOK_TITLE}/logs"
mkdir -p "$LOG_DIR"

# 打印读取到的信息
echo "======================================"
echo "📖 目标书名: $BOOK_TITLE"
echo "📄 目标文件: $PDF"
echo "📂 日志存放: $LOG_DIR"

# PDF 页数检查
PAGE_LIMIT=$($PYTHON_CMD -c "
import yaml
with open('$CONFIG') as f:
    c = yaml.safe_load(f)
print(c.get('refine', {}).get('adaptive_page_limit', {}).get('initial_pages', 900))
" 2>/dev/null || echo "900")
PAGE_COUNT=$($PYTHON_CMD -c "
import fitz; doc=fitz.open('$PDF'); print(doc.page_count)
" 2>/dev/null || echo "?")
echo "📐 PDF 页数: $PAGE_COUNT (refine batch: $PAGE_LIMIT 页/批)"
if [ "$PAGE_COUNT" != "?" ] && [ "$PAGE_COUNT" -gt "$PAGE_LIMIT" ] 2>/dev/null; then
    BATCHES=$(( (PAGE_COUNT + PAGE_LIMIT - 1) / PAGE_LIMIT ))
    echo "   ⚠ PDF 较大，将拆分为 ~$BATCHES 批分析"
fi
echo "======================================"

# 👇 4. 执行流程（全部带 --resume，已完成的部分自动跳过）
echo "=== Step 1: OCR ==="
$PDF2EPUB_CMD -c "$CONFIG" ocr-pages -i "$PDF" --resume 2>&1 | tee "$LOG_DIR/ocr.log"

echo "=== Step 2: Refine ==="
$PDF2EPUB_CMD -c "$CONFIG" refine --resume 2>&1 | tee "$LOG_DIR/refine.log"

# TOC 完整性检查
TOC_FILE="output/${BOOK_TITLE}/toc_tree.json"
if [ -f "$TOC_FILE" ]; then
    python3 -c "
import json, sys

with open('$TOC_FILE') as f:
    data = json.load(f)
chapters = data.get('chapters', [])

def flatten(node):
    n = dict(node)
    n['_depth'] = n.get('_depth', 0)
    result = [n]
    for c in node.get('children', []):
        c['_depth'] = n['_depth'] + 1
        result.extend(flatten(c))
    return result

all_nodes = []
for ch in chapters:
    all_nodes.extend(flatten(ch))

issues = []

# 1. 同层重叠
for i, a in enumerate(all_nodes):
    for j, b in enumerate(all_nodes):
        if i >= j or a['_depth'] != b['_depth']: continue
        if a['start_page'] >= b['end_page'] - 1 or b['start_page'] >= a['end_page'] - 1: continue
        overlap = min(a['end_page'], b['end_page']) - max(a['start_page'], b['start_page']) + 1
        issues.append(f'[重叠{overlap}p] L{a[\"level\"]} \"{a[\"title\"][:45]}\" (p{a[\"start_page\"]}-p{a[\"end_page\"]}) <-> \"{b[\"title\"][:45]}\" (p{b[\"start_page\"]}-p{b[\"end_page\"]})')

# 2. 顺序异常
l1 = [c for c in chapters if c.get('start_page')]
for i in range(1, len(l1)):
    if l1[i]['start_page'] < l1[i-1]['end_page'] - 1:
        issues.append(f'[顺序异常] \"{l1[i][\"title\"][:45]}\" 从 p{l1[i][\"start_page\"]} 开始，但上一章 \"{l1[i-1][\"title\"][:45]}\" 到 p{l1[i-1][\"end_page\"]}')

# 3. 异常大章（仅检测叶节点——有子chapter的Part跨多页正常）
page_count = chapters[-1].get('end_page', 1) if chapters else 1
for n in all_nodes:
    span = n['end_page'] - n['start_page'] + 1
    has_children = bool(n.get('children'))
    if not has_children and span > 100 and n.get('estimated_tokens', 0) > 50000:
        pct = span / max(page_count, 1) * 100
        issues.append(f'[超大章] \"{n[\"title\"][:45]}\" 跨 {span}p ({pct:.0f}% 全书, {n.get(\"estimated_tokens\",0)} tokens) — 可能吞并了后续章节')

# 4. 重复标题（排除常见泛词）
COMMON = {'introduction','conclusion','preface','acknowledgments','acknowledgements',
          'bibliography','references','notes','index','appendix','foreword','epilogue',
          'prologue','afterword','literatur','abbreviations','glossary'}
seen = {}
for n in all_nodes:
    t = n['title'].strip().lower()
    if len(t) > 10 and t not in COMMON:
        if t in seen:
            issues.append(f'[重复标题] \"{n[\"title\"][:45]}\" 出现于 p{n[\"start_page\"]}（首次在 p{seen[t]}）')
        seen[t] = n['start_page']

if issues:
    for issue in issues:
        print(issue)
    print(f'TOTAL_ISSUES: {len(issues)}')
    sys.exit(1)
else:
    print(f'{len(chapters)} chapters, {len(all_nodes)} sections, clean')
    sys.exit(0)
" 2>&1 | tee -a "$LOG_DIR/refine.log"
    TOC_EXIT=$?

    if [ $TOC_EXIT -ne 0 ]; then
        echo ""
        echo "╔═══════════════════════════════════════════╗"
        echo "║   ❌ TOC 检测失败，流程中止               ║"
        echo "╠═══════════════════════════════════════════╣"
        echo "║ 👉 修复步骤：                             ║"
        echo "║   1. 降低 config 中的                      ║"
        echo "║      refine.adaptive_page_limit.initial_pages"
        echo "║   2. 删除 output/${BOOK_TITLE}/refiner_state.json"
        echo "║   3. 删除 output/${BOOK_TITLE}/ocr_markdown/"
        echo "║   4. 重新运行: bash ocr_polish.sh          ║"
        echo "╚═══════════════════════════════════════════╝"
        exit 1
    fi
    echo "✅ TOC 检测通过"
fi

echo "=== Step 3: Polish ==="
$PDF2EPUB_CMD -c "$CONFIG" polish --resume 2>&1 | tee "$LOG_DIR/polish.log"

echo "=== Step 4: Build EPUB ==="
$PDF2EPUB_CMD -c "$CONFIG" build-epub 2>&1 | tee "$LOG_DIR/build_raw.log"

echo "=== ALL DONE ==="
date
