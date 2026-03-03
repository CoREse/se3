#!/bin/bash
#
# SE3 测试项目重置脚本
# 用于将测试项目恢复到测试前的干净状态
#

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo -e "${YELLOW}=== SE3 测试项目重置脚本 ===${NC}"
echo "项目目录: $PROJECT_DIR"
cd "$PROJECT_DIR"

# 1. 检查 git 状态
echo -e "\n${YELLOW}[1/5] 检查 Git 状态...${NC}"
if [ ! -d ".git" ]; then
    echo -e "${RED}错误: 不是 git 仓库${NC}"
    exit 1
fi

# 保存当前分支
CURRENT_BRANCH=$(git branch --show-current)
echo "当前分支: $CURRENT_BRANCH"

# 2. 重置到初始 SE3 配置提交
echo -e "\n${YELLOW}[2/5] 重置到初始提交...${NC}"
INITIAL_COMMIT="5401add"
if git rev-parse --verify "$INITIAL_COMMIT" > /dev/null 2>&1; then
    git reset --hard "$INITIAL_COMMIT"
    echo -e "${GREEN}✓ 已重置到提交 $INITIAL_COMMIT${NC}"
else
    echo -e "${RED}错误: 找不到初始提交 $INITIAL_COMMIT${NC}"
    echo "请检查 git log 确认正确的提交 hash"
    exit 1
fi

# 3. 清理未跟踪的文件和目录
echo -e "\n${YELLOW}[3/5] 清理未跟踪文件...${NC}"

# 清理 SE3 运行时文件/目录
SE3_RUNTIME_DIRS=(
    "se3/state"
    "se3/tmp"
    "se3/logs"
    "se3/cache"
    "se3/history"
    "se3/calls/active"
    "se3/collab"
)

for dir in "${SE3_RUNTIME_DIRS[@]}"; do
    if [ -d "$dir" ]; then
        rm -rf "$dir"/*
        echo "  清理: $dir"
    fi
done

# 清理生成的文档
GENERATED_FILES=(
    "progress.md"
    "VERSIONS.md"
    "tasks.json"
)

for file in "${GENERATED_FILES[@]}"; do
    if [ -f "$file" ]; then
        rm -f "$file"
        echo "  删除: $file"
    fi
done

# 使用 git clean 清理其他未跟踪文件
git clean -fd

echo -e "${GREEN}✓ 清理完成${NC}"

# 4. 验证项目状态
echo -e "\n${YELLOW}[4/5] 验证项目状态...${NC}"

# 检查 git 状态
if git diff --quiet && git diff --cached --quiet; then
    echo -e "${GREEN}✓ Git 工作区干净${NC}"
else
    echo -e "${RED}✗ Git 工作区有变更${NC}"
    git status
    exit 1
fi

# 检查关键文件存在
KEY_FILES=(
    "pyproject.toml"
    "README.md"
    "se3.yaml"
    "se3/specs/base/spec.md"
    "se3/specs/task-cli/spec.md"
    "src/task_cli/cli.py"
    "tests/test_cli.py"
)

for file in "${KEY_FILES[@]}"; do
    if [ -f "$file" ]; then
        echo -e "${GREEN}✓${NC} $file"
    else
        echo -e "${RED}✗ 缺失: $file${NC}"
        exit 1
    fi
done

# 检查版本
VERSION=$(grep -E '^version\s*=\s*"' pyproject.toml | head -1 | sed 's/.*"\([^"]*\)".*/\1/')
if [ "$VERSION" = "0.1.0" ]; then
    echo -e "${GREEN}✓ 版本正确: $VERSION${NC}"
else
    echo -e "${RED}✗ 版本不正确: $VERSION (期望 0.1.0)${NC}"
    exit 1
fi

# 5. 显示最终状态
echo -e "\n${YELLOW}[5/5] 最终状态...${NC}"
echo ""
echo "Git 状态:"
git log --oneline -3
echo ""
echo "目录结构:"
ls -la
echo ""
echo -e "${GREEN}=== 重置完成 ===${NC}"
echo ""
echo "项目已恢复到测试前的干净状态。"
echo "可以开始新的测试。"
