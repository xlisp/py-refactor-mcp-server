# PyRefactor Analyzer

Python 代码重构审查建议工具 —— 静态扫描你的 Python 项目，自动发现代码坏味道，给出重构建议，并生成精美的 HTML 可视化报告。

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
- 无第三方依赖（仅使用标准库 `ast`、`json`、`os`、`pathlib` 等）
- HTML 报告使用 CDN 加载 [Chart.js](https://www.chartjs.org/)（需联网查看图表）

## 安装

无需安装，直接克隆或下载 `refactor_analyzer.py` 即可使用：

```bash
git clone <repo-url>
cd py-refactor
```

## 使用方法

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

## 试用示例项目

仓库自带一个 `sample_project/`，包含常见代码坏味道，可直接测试：

```bash
python refactor_analyzer.py sample_project -o demo_report.html
```

在浏览器中打开 `demo_report.html` 查看效果。

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

- 80-100：优秀
- 60-79：一般
- 0-59：需改进

## 自动排除目录

扫描时自动跳过以下目录：

```
.venv  venv  __pycache__  .git  node_modules
.mypy_cache  .pytest_cache  dist  build  .eggs
```

## 作为库使用

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

## 项目结构

```
py-refactor/
├── refactor_analyzer.py    # 主程序（分析器 + 报告生成）
├── sample_project/         # 示例项目（包含各种代码坏味道）
│   └── app.py
└── README.md
```

## License

MIT
