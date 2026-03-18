# PyRefactor Analyzer

Python 代码重构审查建议工具 —— 静态扫描你的 Python 项目，自动发现代码坏味道，给出重构建议，并生成精美的 HTML 可视化报告。

提供两种使用方式：**命令行工具** 和 **MCP Server**（可接入 Claude Desktop / VSCode / Cursor 等 AI 编辑器）。

## 功能特性

### 代码问题检测

| 检测项 | 说明 | 默认阈值 |
|--------|------|----------|
| 函数过长 | 函数体行数超过阈值 | 30 行 |
| 参数过多 | 函数签名参数过多 | 5 个 |
| 局部变量过多 | 单函数内变量定义过多，建议提取 | 8 个 |
| 圈复杂度高 | McCabe 复杂度超标 | 10 |
| 文件过长 | 单文件代码行数过多 | 400 行 |
| 单文件类过多 | 一个文件中定义太多类 | 4 个 |
| 单文件函数过多 | 一个文件中顶层函数太多 | 10 个 |

### 智能重组建议

- **函数前缀聚类**：自动识别共享前缀的函数（如 `data_load_csv`、`data_load_json`），建议提取到独立模块（`data_utils.py`）
- **类拆分建议**：当单文件类过多时，建议将每个类独立为单独模块（`UserManager` → `user_manager.py`）

### HTML 可视化报告

- 项目健康评分（0-100 分）
- 统计概览仪表盘（文件数、行数、函数数、问题数）
- 问题分类分布饼图
- 函数长度 TOP 15 横向柱状图
- 圈复杂度 TOP 15 横向柱状图
- 文件大小分布柱状图
- 问题详情表格（支持按严重度 / 类型 / 关键字筛选）
- 文件重组建议卡片
- 暗色主题，响应式布局，移动端友好

## 环境要求

- Python 3.10+
- 命令行工具：无第三方依赖（仅标准库）
- MCP Server：需安装 `mcp[cli]`
- HTML 报告使用 CDN 加载 [Chart.js](https://www.chartjs.org/)（需联网查看图表）

## 安装

```bash
git clone <repo-url>
cd py-refactor

# MCP Server 需要额外安装依赖
pip install "mcp[cli]"
```

---

## 方式一：命令行工具

### 基本用法

```bash
# 扫描当前目录
python refactor_analyzer.py .

# 扫描指定项目目录
python refactor_analyzer.py /path/to/your/project

# 指定输出文件名
python refactor_analyzer.py /path/to/project -o my_report.html
```

### 自定义阈值

```bash
# 放宽函数长度限制到 50 行，复杂度到 15
python refactor_analyzer.py . --max-func-lines 50 --max-complexity 15

# 严格模式：更低的阈值
python refactor_analyzer.py . --max-func-lines 20 --max-params 3 --max-vars 5
```

### 完整参数列表

```
用法: refactor_analyzer.py [-h] [-o OUTPUT] [--max-func-lines N]
                           [--max-params N] [--max-vars N]
                           [--max-complexity N] [--max-file-lines N]
                           project_dir

位置参数:
  project_dir             要扫描的 Python 项目目录

可选参数:
  -h, --help              显示帮助信息
  -o, --output OUTPUT     输出 HTML 报告路径 (默认: refactor_report.html)
  --max-func-lines N      函数最大行数阈值 (默认: 30)
  --max-params N          函数最大参数数 (默认: 5)
  --max-vars N            局部变量最大数 (默认: 8)
  --max-complexity N      圈复杂度阈值 (默认: 10)
  --max-file-lines N      文件最大行数 (默认: 400)
```

### 终端输出示例

```
正在扫描: /home/user/my-project
扫描完成:
  文件数: 23
  总行数: 4,567
  函数数: 189
  问题数: 17
  重组建议: 5 条

报告已生成: /home/user/my-project/refactor_report.html
```

### 作为库使用

```python
from refactor_analyzer import scan_project, generate_html_report

# 扫描项目
result = scan_project("/path/to/project")

# 访问分析结果
print(f"发现 {len(result.issues)} 个问题")
for issue in result.issues:
    print(f"  [{issue.severity}] {issue.file}:{issue.line} - {issue.title}")

# 查看重组建议
for s in result.reorg_suggestions:
    print(f"  {s.source_file} → {s.suggested_file}: {', '.join(s.items)}")

# 生成 HTML 报告
generate_html_report(result, "report.html")
```

---

## 方式二：MCP Server

MCP Server 让 AI 助手（Claude Desktop、VSCode Copilot、Cursor 等）直接调用代码审查能力，在对话中即时分析项目质量。

### 启动 MCP Server

```bash
python refactor_mcp_server.py
```

Server 以 `stdio` 模式运行，等待 MCP 客户端连接。

### 配置 Claude Desktop

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`（macOS）或 `%APPDATA%\Claude\claude_desktop_config.json`（Windows）：

```json
{
  "mcpServers": {
    "py-refactor": {
      "command": "python",
      "args": ["/absolute/path/to/refactor_mcp_server.py"]
    }
  }
}
```

### 配置 VSCode

在项目根目录创建 `.vscode/mcp.json`：

```json
{
  "servers": {
    "py-refactor": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/refactor_mcp_server.py"]
    }
  }
}
```

### 配置 Cursor / Windsurf

在 MCP 设置中添加：

```json
{
  "mcpServers": {
    "py-refactor": {
      "command": "python",
      "args": ["/absolute/path/to/refactor_mcp_server.py"]
    }
  }
}
```

### MCP Tools 一览

Server 提供 **7 个工具**，AI 助手可在对话中按需调用：

| Tool | 功能 | 典型用法 |
|------|------|----------|
| `scan_project` | 全项目扫描，返回所有问题 + 重组建议 + 健康评分 | "帮我扫描一下这个项目的代码质量" |
| `analyze_file` | 分析单个文件，列出函数指标排行 | "分析一下 utils.py 的代码质量" |
| `analyze_function` | 深度分析单个函数，给出具体重构建议 | "process_order 这个函数怎么重构比较好" |
| `find_long_functions` | 查找最长函数 TOP N | "找出项目中最长的 10 个函数" |
| `find_complex_functions` | 查找最高复杂度函数 TOP N | "哪些函数复杂度最高" |
| `suggest_file_reorg` | 文件重组建议 | "这个项目的文件结构需要调整吗" |
| `generate_report` | 生成 HTML 可视化报告 | "生成一份完整的代码质量报告" |
| `health_score` | 快速健康评分 0-100 | "这个项目代码质量怎么样" |

### Tool 参数详情

#### `scan_project`

全面扫描项目，返回完整的问题列表和重构建议。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `project_dir` | string | 必填 | 项目目录绝对路径 |
| `max_func_lines` | int | 30 | 函数行数阈值 |
| `max_func_params` | int | 5 | 参数数量阈值 |
| `max_local_vars` | int | 8 | 局部变量数量阈值 |
| `max_complexity` | int | 10 | 圈复杂度阈值 |
| `max_file_lines` | int | 400 | 文件行数阈值 |

#### `analyze_function`

深度分析指定函数。

| 参数 | 类型 | 说明 |
|------|------|------|
| `file_path` | string | Python 文件绝对路径 |
| `function_name` | string | 函数名，方法用 `ClassName.method_name` 格式 |

#### `find_long_functions` / `find_complex_functions`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `project_dir` | string | 必填 | 项目目录绝对路径 |
| `min_lines` / `min_complexity` | int | 30 / 10 | 最低阈值 |
| `top_n` | int | 20 | 返回结果数量上限 |

#### `generate_report`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `project_dir` | string | 必填 | 项目目录绝对路径 |
| `output_path` | string | `refactor_report.html` | 输出 HTML 路径 |
| `max_func_lines` | int | 30 | 函数行数阈值 |
| `max_complexity` | int | 10 | 复杂度阈值 |

### 对话使用示例

配置好 MCP Server 后，在 AI 助手对话中直接使用自然语言：

```
用户: 帮我扫描 /home/user/my-project 这个项目的代码质量

AI: (调用 scan_project) 项目健康评分 72/100，发现 17 个问题...
    高严重度: process_user_data 函数 89 行，建议拆分为...

用户: process_user_data 这个函数具体怎么重构？

AI: (调用 analyze_function) 该函数有 89 行、12 个局部变量、
    圈复杂度 18，建议：
    1. 将验证逻辑提取为 _validate_user()
    2. 用字典映射替代 action 的 if-elif 分支...

用户: 生成一份完整的 HTML 报告

AI: (调用 generate_report) 报告已生成: /home/user/refactor_report.html
```

---

## 试用示例项目

仓库自带一个 `sample_project/`，包含常见代码坏味道，可直接测试：

```bash
# 命令行方式
python refactor_analyzer.py sample_project -o demo_report.html

# 在浏览器中打开 demo_report.html 查看效果
```

## 问题严重度说明

| 等级 | 颜色 | 含义 |
|------|------|------|
| **高** | 红色 | 函数行数超阈值 2 倍，或复杂度超阈值 2 倍 |
| **中** | 黄色 | 超过阈值但未达 2 倍，参数过多，变量过多等 |
| **低** | 绿色 | 单文件函数数量偏多等轻度问题 |

## 健康评分算法

```
score = 100 - (高严重度问题数 × 8 + 其他问题数 × 3) × 100 / (函数总数 × 10)
```

| 分数 | 等级 | 评价 |
|------|------|------|
| 80-100 | A | 优秀 - 代码质量良好 |
| 60-79 | B | 一般 - 建议关注高优问题 |
| 40-59 | C | 需改进 - 存在较多质量问题 |
| 0-39 | D | 较差 - 强烈建议重构 |

## 自动排除目录

扫描时自动跳过以下目录：

```
.venv  venv  env  __pycache__  .git  node_modules
.mypy_cache  .pytest_cache  dist  build  .eggs
```

## 项目结构

```
py-refactor/
├── refactor_analyzer.py      # 命令行工具（分析器 + HTML 报告生成）
├── refactor_mcp_server.py    # MCP Server（7 个 AI 可调用的工具）
├── sample_project/           # 示例项目（包含各种代码坏味道）
│   └── app.py
└── README.md
```

## License

MIT
