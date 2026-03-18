#!/usr/bin/env python3
"""
Python 代码重构审查建议工具
扫描 Python 项目，分析代码质量并生成 HTML 可视化报告。

功能：
1. 检测过长的函数定义
2. 检测可以提取的变量定义
3. 建议文件/函数重组方案
4. 生成 HTML 可视化报告
"""

import ast
import os
import sys
import json
import textwrap
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict
from pathlib import Path


# ─── 配置阈值 ───────────────────────────────────────────────────────────────
MAX_FUNC_LINES = 30          # 函数体超过此行数视为过长
MAX_FUNC_PARAMS = 5          # 参数超过此数量视为过多
MAX_LOCAL_VARS = 8           # 局部变量超过此数量建议提取
MAX_COMPLEXITY = 10          # 圈复杂度阈值
MAX_FILE_LINES = 400         # 文件超过此行数建议拆分
MAX_CLASSES_PER_FILE = 4     # 单文件类数量上限
MAX_FUNCS_PER_FILE = 10      # 单文件顶层函数数量上限
MIN_SIMILAR_PREFIX = 2       # 可归组函数的最少数量


# ─── 数据结构 ───────────────────────────────────────────────────────────────
@dataclass
class Issue:
    file: str
    line: int
    category: str       # long_func | too_many_params | extract_vars | high_complexity
    severity: str        # high | medium | low
    title: str
    detail: str
    suggestion: str


@dataclass
class ReorgSuggestion:
    source_file: str
    items: list          # 函数/类名列表
    suggested_file: str
    reason: str


@dataclass
class FuncInfo:
    name: str
    file: str
    line: int
    end_line: int
    num_lines: int
    num_params: int
    local_vars: list
    complexity: int
    decorators: list
    is_method: bool
    class_name: Optional[str]


@dataclass
class FileInfo:
    path: str
    total_lines: int
    classes: list
    top_functions: list
    imports: list


@dataclass
class AnalysisResult:
    files_analyzed: int = 0
    total_lines: int = 0
    issues: list = field(default_factory=list)
    reorg_suggestions: list = field(default_factory=list)
    file_infos: list = field(default_factory=list)
    func_infos: list = field(default_factory=list)


# ─── AST 分析器 ─────────────────────────────────────────────────────────────
class CodeAnalyzer(ast.NodeVisitor):
    """遍历 AST 收集函数和类信息。"""

    def __init__(self, filepath: str, source: str):
        self.filepath = filepath
        self.source = source
        self.source_lines = source.splitlines()
        self.functions: list[FuncInfo] = []
        self.classes: list[str] = []
        self.top_functions: list[str] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        module = node.module or ""
        for alias in node.names:
            self.imports.append(f"{module}.{alias.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        self.classes.append(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._analyze_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _analyze_function(self, node):
        is_method = len(self._class_stack) > 0
        class_name = self._class_stack[-1] if is_method else None

        if not is_method:
            self.top_functions.append(node.name)

        # 计算行数
        end_line = node.end_lineno or node.lineno
        num_lines = end_line - node.lineno + 1

        # 参数数量 (排除 self/cls)
        args = node.args
        all_args = args.args + args.posonlyargs + args.kwonlyargs
        param_names = [a.arg for a in all_args]
        if is_method and param_names and param_names[0] in ("self", "cls"):
            param_names = param_names[1:]
        num_params = len(param_names)
        if args.vararg:
            num_params += 1
        if args.kwarg:
            num_params += 1

        # 收集局部变量 (赋值目标)
        local_vars = self._collect_local_vars(node)

        # 圈复杂度
        complexity = self._calc_complexity(node)

        # 装饰器
        decorators = []
        for d in node.decorator_list:
            if isinstance(d, ast.Name):
                decorators.append(d.id)
            elif isinstance(d, ast.Attribute):
                decorators.append(d.attr)

        self.functions.append(FuncInfo(
            name=node.name,
            file=self.filepath,
            line=node.lineno,
            end_line=end_line,
            num_lines=num_lines,
            num_params=num_params,
            local_vars=local_vars,
            complexity=complexity,
            decorators=decorators,
            is_method=is_method,
            class_name=class_name,
        ))

    def _collect_local_vars(self, func_node) -> list[str]:
        """收集函数体内的局部变量名。"""
        vars_found = set()
        for node in ast.walk(func_node):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        vars_found.add(target.id)
                    elif isinstance(target, ast.Tuple):
                        for elt in target.elts:
                            if isinstance(elt, ast.Name):
                                vars_found.add(elt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                vars_found.add(node.target.id)
        return sorted(vars_found)

    def _calc_complexity(self, node) -> int:
        """计算 McCabe 圈复杂度。"""
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For,
                                  ast.AsyncFor, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
                complexity += len(child.ifs)
        return complexity


# ─── 主分析引擎 ─────────────────────────────────────────────────────────────
def scan_project(root_dir: str, exclude_dirs: set[str] | None = None) -> AnalysisResult:
    """扫描整个项目目录，返回分析结果。"""
    if exclude_dirs is None:
        exclude_dirs = {".venv", "venv", "__pycache__", ".git", "node_modules",
                        ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}

    result = AnalysisResult()
    root = Path(root_dir).resolve()

    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))

    for filepath in sorted(py_files):
        try:
            source = Path(filepath).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel_path = os.path.relpath(filepath, root)
        lines = source.splitlines()
        result.total_lines += len(lines)
        result.files_analyzed += 1

        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError:
            continue

        analyzer = CodeAnalyzer(rel_path, source)
        analyzer.visit(tree)

        fi = FileInfo(
            path=rel_path,
            total_lines=len(lines),
            classes=analyzer.classes,
            top_functions=analyzer.top_functions,
            imports=analyzer.imports,
        )
        result.file_infos.append(fi)
        result.func_infos.extend(analyzer.functions)

        # ── 生成问题报告 ──
        _check_file_issues(fi, result)
        for func in analyzer.functions:
            _check_func_issues(func, result)

    # ── 文件重组建议 ──
    _generate_reorg_suggestions(result)

    return result


def _check_file_issues(fi: FileInfo, result: AnalysisResult):
    """检查文件级别的问题。"""
    if fi.total_lines > MAX_FILE_LINES:
        result.issues.append(Issue(
            file=fi.path, line=1,
            category="long_file",
            severity="medium",
            title=f"文件过长 ({fi.total_lines} 行)",
            detail=f"文件 {fi.path} 共 {fi.total_lines} 行，超过阈值 {MAX_FILE_LINES} 行。",
            suggestion="建议按功能拆分为多个模块文件。",
        ))

    if len(fi.classes) > MAX_CLASSES_PER_FILE:
        result.issues.append(Issue(
            file=fi.path, line=1,
            category="too_many_classes",
            severity="medium",
            title=f"单文件类过多 ({len(fi.classes)} 个)",
            detail=f"文件包含 {len(fi.classes)} 个类: {', '.join(fi.classes)}",
            suggestion="建议每个类（或紧密相关的类）放入独立文件中。",
        ))

    if len(fi.top_functions) > MAX_FUNCS_PER_FILE:
        result.issues.append(Issue(
            file=fi.path, line=1,
            category="too_many_funcs",
            severity="low",
            title=f"单文件函数过多 ({len(fi.top_functions)} 个)",
            detail=f"文件包含 {len(fi.top_functions)} 个顶层函数。",
            suggestion="建议按职责将函数分组到不同模块。",
        ))


def _check_func_issues(func: FuncInfo, result: AnalysisResult):
    """检查函数级别的问题。"""
    display_name = f"{func.class_name}.{func.name}" if func.class_name else func.name

    # 函数过长
    if func.num_lines > MAX_FUNC_LINES:
        result.issues.append(Issue(
            file=func.file, line=func.line,
            category="long_func",
            severity="high" if func.num_lines > MAX_FUNC_LINES * 2 else "medium",
            title=f"函数过长: {display_name} ({func.num_lines} 行)",
            detail=f"函数 {display_name} 共 {func.num_lines} 行 (第{func.line}-{func.end_line}行)，超过阈值 {MAX_FUNC_LINES} 行。",
            suggestion="建议将函数拆分为多个小函数，每个函数只做一件事。",
        ))

    # 参数过多
    if func.num_params > MAX_FUNC_PARAMS:
        result.issues.append(Issue(
            file=func.file, line=func.line,
            category="too_many_params",
            severity="medium",
            title=f"参数过多: {display_name} ({func.num_params} 个参数)",
            detail=f"函数 {display_name} 有 {func.num_params} 个参数，超过阈值 {MAX_FUNC_PARAMS}。",
            suggestion="建议使用 dataclass 或 TypedDict 封装参数，或拆分函数职责。",
        ))

    # 局部变量过多 → 建议提取
    if len(func.local_vars) > MAX_LOCAL_VARS:
        result.issues.append(Issue(
            file=func.file, line=func.line,
            category="extract_vars",
            severity="medium",
            title=f"局部变量过多: {display_name} ({len(func.local_vars)} 个变量)",
            detail=f"变量列表: {', '.join(func.local_vars[:15])}{'...' if len(func.local_vars)>15 else ''}",
            suggestion="建议将相关变量和逻辑提取为独立函数或数据类，降低单函数复杂度。",
        ))

    # 圈复杂度过高
    if func.complexity > MAX_COMPLEXITY:
        result.issues.append(Issue(
            file=func.file, line=func.line,
            category="high_complexity",
            severity="high" if func.complexity > MAX_COMPLEXITY * 2 else "medium",
            title=f"圈复杂度过高: {display_name} (复杂度 {func.complexity})",
            detail=f"函数 {display_name} 的 McCabe 圈复杂度为 {func.complexity}，阈值为 {MAX_COMPLEXITY}。",
            suggestion="建议使用早返回、策略模式或将条件分支提取为子函数来降低复杂度。",
        ))


def _generate_reorg_suggestions(result: AnalysisResult):
    """基于函数名前缀和功能聚类，生成文件重组建议。"""
    # 按文件分组顶层函数
    file_funcs: dict[str, list[FuncInfo]] = defaultdict(list)
    for f in result.func_infos:
        if not f.is_method:
            file_funcs[f.file].append(f)

    for filepath, funcs in file_funcs.items():
        if len(funcs) < MIN_SIMILAR_PREFIX * 2:
            continue

        # 按前缀分组
        prefix_groups: dict[str, list[str]] = defaultdict(list)
        for f in funcs:
            parts = f.name.split("_")
            if len(parts) >= 2 and not f.name.startswith("_"):
                prefix = parts[0]
                prefix_groups[prefix].append(f.name)

        for prefix, names in prefix_groups.items():
            if len(names) >= MIN_SIMILAR_PREFIX:
                result.reorg_suggestions.append(ReorgSuggestion(
                    source_file=filepath,
                    items=names,
                    suggested_file=f"{prefix}_utils.py",
                    reason=f"这 {len(names)} 个函数共享前缀 '{prefix}_'，功能可能相关，"
                           f"建议提取到独立模块。",
                ))

    # 对包含过多类的文件，建议每个类独立
    for fi in result.file_infos:
        if len(fi.classes) > MAX_CLASSES_PER_FILE:
            for cls_name in fi.classes:
                result.reorg_suggestions.append(ReorgSuggestion(
                    source_file=fi.path,
                    items=[cls_name],
                    suggested_file=f"{_camel_to_snake(cls_name)}.py",
                    reason=f"类 {cls_name} 可独立为单独模块，降低文件复杂度。",
                ))


def _camel_to_snake(name: str) -> str:
    """CamelCase → snake_case"""
    result = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            result.append("_")
        result.append(ch.lower())
    return "".join(result)


# ─── HTML 报告生成 ───────────────────────────────────────────────────────────
def generate_html_report(result: AnalysisResult, output_path: str):
    """生成可视化 HTML 报告。"""

    severity_counts = {"high": 0, "medium": 0, "low": 0}
    category_counts = defaultdict(int)
    for iss in result.issues:
        severity_counts[iss.severity] += 1
        category_counts[iss.category] += 1

    # 按文件分组 issues
    file_issues = defaultdict(list)
    for iss in result.issues:
        file_issues[iss.file].append(iss)

    # 函数长度排行
    top_long_funcs = sorted(result.func_infos, key=lambda f: f.num_lines, reverse=True)[:20]
    # 复杂度排行
    top_complex_funcs = sorted(result.func_infos, key=lambda f: f.complexity, reverse=True)[:20]

    # 为图表准备数据
    category_labels = {
        "long_func": "函数过长",
        "too_many_params": "参数过多",
        "extract_vars": "变量过多",
        "high_complexity": "复杂度高",
        "long_file": "文件过长",
        "too_many_classes": "类过多",
        "too_many_funcs": "函数过多",
    }

    chart_categories = json.dumps([category_labels.get(k, k) for k in category_counts.keys()],
                                  ensure_ascii=False)
    chart_values = json.dumps(list(category_counts.values()))

    func_names_chart = json.dumps(
        [f"{f.class_name}.{f.name}" if f.class_name else f.name for f in top_long_funcs[:15]],
        ensure_ascii=False)
    func_lines_chart = json.dumps([f.num_lines for f in top_long_funcs[:15]])

    complex_names = json.dumps(
        [f"{f.class_name}.{f.name}" if f.class_name else f.name for f in top_complex_funcs[:15]],
        ensure_ascii=False)
    complex_values = json.dumps([f.complexity for f in top_complex_funcs[:15]])

    # 文件行数 treemap 数据
    file_sizes = json.dumps(
        [{"name": fi.path, "value": fi.total_lines} for fi in
         sorted(result.file_infos, key=lambda x: x.total_lines, reverse=True)[:30]],
        ensure_ascii=False)

    issues_html_rows = []
    for iss in sorted(result.issues, key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i.severity], i.file)):
        sev_class = {"high": "sev-high", "medium": "sev-med", "low": "sev-low"}[iss.severity]
        sev_label = {"high": "高", "medium": "中", "low": "低"}[iss.severity]
        cat_label = category_labels.get(iss.category, iss.category)
        issues_html_rows.append(f"""
        <tr>
          <td><span class="badge {sev_class}">{sev_label}</span></td>
          <td><span class="badge badge-cat">{cat_label}</span></td>
          <td class="file-cell">{iss.file}:{iss.line}</td>
          <td><strong>{iss.title}</strong><br><small class="text-muted">{iss.detail}</small></td>
          <td class="suggestion-cell">{iss.suggestion}</td>
        </tr>""")

    reorg_html = []
    for s in result.reorg_suggestions:
        reorg_html.append(f"""
        <div class="reorg-card">
          <div class="reorg-header">
            <span class="reorg-from">{s.source_file}</span>
            <span class="reorg-arrow">→</span>
            <span class="reorg-to">{s.suggested_file}</span>
          </div>
          <div class="reorg-items">移动项: {', '.join(s.items)}</div>
          <div class="reorg-reason">{s.reason}</div>
        </div>""")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Python 代码重构审查报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg: #0f172a;
  --surface: #1e293b;
  --surface2: #334155;
  --border: #475569;
  --text: #e2e8f0;
  --text-muted: #94a3b8;
  --accent: #38bdf8;
  --accent2: #818cf8;
  --danger: #f87171;
  --warning: #fbbf24;
  --success: #34d399;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}}
.container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}

/* Header */
.header {{
  text-align: center;
  padding: 40px 20px;
  background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
  border-bottom: 1px solid var(--border);
  margin-bottom: 30px;
}}
.header h1 {{
  font-size: 2.2em;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  margin-bottom: 8px;
}}
.header .subtitle {{ color: var(--text-muted); font-size: 1.1em; }}

/* Stats bar */
.stats-bar {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 30px;
}}
.stat-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
  text-align: center;
}}
.stat-card .stat-num {{
  font-size: 2.4em;
  font-weight: 700;
  line-height: 1.2;
}}
.stat-card .stat-label {{
  color: var(--text-muted);
  font-size: 0.9em;
  margin-top: 4px;
}}
.stat-num.text-danger {{ color: var(--danger); }}
.stat-num.text-warning {{ color: var(--warning); }}
.stat-num.text-success {{ color: var(--success); }}
.stat-num.text-accent {{ color: var(--accent); }}

/* Sections */
.section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  margin-bottom: 24px;
  overflow: hidden;
}}
.section-title {{
  padding: 16px 24px;
  font-size: 1.3em;
  font-weight: 600;
  border-bottom: 1px solid var(--border);
  background: var(--surface2);
}}

/* Charts grid */
.charts-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
  gap: 24px;
  margin-bottom: 24px;
}}
.chart-box {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px;
}}
.chart-box h3 {{
  margin-bottom: 16px;
  font-size: 1.1em;
  color: var(--accent);
}}
.chart-box canvas {{ max-height: 350px; }}

/* Table */
.issues-table {{
  width: 100%;
  border-collapse: collapse;
}}
.issues-table th {{
  background: var(--surface2);
  padding: 12px 16px;
  text-align: left;
  font-weight: 600;
  font-size: 0.85em;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  position: sticky;
  top: 0;
}}
.issues-table td {{
  padding: 12px 16px;
  border-top: 1px solid var(--border);
  vertical-align: top;
  font-size: 0.92em;
}}
.issues-table tr:hover {{ background: rgba(56,189,248,0.05); }}
.file-cell {{ font-family: 'Fira Code', monospace; font-size: 0.85em; color: var(--accent); white-space: nowrap; }}
.suggestion-cell {{ color: var(--success); }}
.text-muted {{ color: var(--text-muted); }}

/* Badges */
.badge {{
  display: inline-block;
  padding: 3px 10px;
  border-radius: 20px;
  font-size: 0.8em;
  font-weight: 600;
  white-space: nowrap;
}}
.sev-high {{ background: rgba(248,113,113,0.2); color: var(--danger); border: 1px solid var(--danger); }}
.sev-med  {{ background: rgba(251,191,36,0.2); color: var(--warning); border: 1px solid var(--warning); }}
.sev-low  {{ background: rgba(52,211,153,0.2); color: var(--success); border: 1px solid var(--success); }}
.badge-cat {{ background: rgba(129,140,248,0.15); color: var(--accent2); border: 1px solid var(--accent2); }}

/* Reorg cards */
.reorg-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
  gap: 16px;
  padding: 20px;
}}
.reorg-card {{
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}}
.reorg-header {{
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}}
.reorg-from {{ font-family: monospace; color: var(--danger); }}
.reorg-arrow {{ color: var(--accent); font-size: 1.4em; }}
.reorg-to {{ font-family: monospace; color: var(--success); font-weight: 600; }}
.reorg-items {{ font-size: 0.88em; color: var(--text-muted); margin-bottom: 6px; }}
.reorg-reason {{ font-size: 0.9em; color: var(--accent2); }}

/* Filter bar */
.filter-bar {{
  padding: 16px 24px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  border-bottom: 1px solid var(--border);
}}
.filter-bar label {{ color: var(--text-muted); font-size: 0.9em; }}
.filter-bar select, .filter-bar input {{
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 0.9em;
}}
.no-issues {{
  text-align: center;
  padding: 60px 20px;
  color: var(--success);
  font-size: 1.3em;
}}

/* Scrollable table wrapper */
.table-wrap {{ max-height: 600px; overflow-y: auto; }}

/* Responsive */
@media (max-width: 768px) {{
  .charts-grid {{ grid-template-columns: 1fr; }}
  .stats-bar {{ grid-template-columns: repeat(2, 1fr); }}
  .reorg-grid {{ grid-template-columns: 1fr; }}
  .container {{ padding: 10px; }}
}}

/* 健康评分仪表盘 */
.health-gauge {{
  width: 200px;
  height: 200px;
  margin: 20px auto;
  position: relative;
}}
.health-score {{
  font-size: 3em;
  font-weight: 700;
  text-align: center;
  padding-top: 10px;
}}
.health-label {{
  text-align: center;
  color: var(--text-muted);
  font-size: 1.1em;
}}
</style>
</head>
<body>

<div class="header">
  <h1>Python 代码重构审查报告</h1>
  <p class="subtitle">自动扫描分析 · 智能重构建议 · 可视化展示</p>
</div>

<div class="container">

<!-- ▸ 统计概览 -->
<div class="stats-bar">
  <div class="stat-card">
    <div class="stat-num text-accent">{result.files_analyzed}</div>
    <div class="stat-label">扫描文件数</div>
  </div>
  <div class="stat-card">
    <div class="stat-num text-accent">{result.total_lines:,}</div>
    <div class="stat-label">总代码行数</div>
  </div>
  <div class="stat-card">
    <div class="stat-num text-accent">{len(result.func_infos)}</div>
    <div class="stat-label">函数/方法数</div>
  </div>
  <div class="stat-card">
    <div class="stat-num text-danger">{severity_counts['high']}</div>
    <div class="stat-label">高严重度问题</div>
  </div>
  <div class="stat-card">
    <div class="stat-num text-warning">{severity_counts['medium']}</div>
    <div class="stat-label">中严重度问题</div>
  </div>
  <div class="stat-card">
    <div class="stat-num text-success">{severity_counts['low']}</div>
    <div class="stat-label">低严重度问题</div>
  </div>
</div>

<!-- ▸ 健康评分 -->
<div class="section">
  <div class="section-title">项目健康评分</div>
  <div style="padding: 20px; text-align: center;">
    <div class="health-score" id="healthScore">--</div>
    <div class="health-label" id="healthLabel">计算中...</div>
  </div>
</div>

<!-- ▸ 图表 -->
<div class="charts-grid">
  <div class="chart-box">
    <h3>问题分类分布</h3>
    <canvas id="chartCategory"></canvas>
  </div>
  <div class="chart-box">
    <h3>函数长度 TOP 15</h3>
    <canvas id="chartFuncLen"></canvas>
  </div>
  <div class="chart-box">
    <h3>圈复杂度 TOP 15</h3>
    <canvas id="chartComplexity"></canvas>
  </div>
  <div class="chart-box">
    <h3>文件大小分布 (行数)</h3>
    <canvas id="chartFileSize"></canvas>
  </div>
</div>

<!-- ▸ 问题详情 -->
<div class="section">
  <div class="section-title">问题详情 ({len(result.issues)} 个问题)</div>
  <div class="filter-bar">
    <label>严重度:</label>
    <select id="filterSev" onchange="filterTable()">
      <option value="all">全部</option>
      <option value="高">高</option>
      <option value="中">中</option>
      <option value="低">低</option>
    </select>
    <label>类型:</label>
    <select id="filterCat" onchange="filterTable()">
      <option value="all">全部</option>
      {"".join(f'<option value="{v}">{v}</option>' for v in category_labels.values())}
    </select>
    <label>搜索:</label>
    <input type="text" id="filterSearch" placeholder="文件名或函数名..." oninput="filterTable()">
  </div>
  {"<div class='no-issues'>没有发现问题，代码质量良好！</div>" if not result.issues else ""}
  <div class="table-wrap">
    <table class="issues-table" id="issuesTable">
      <thead><tr>
        <th>严重度</th><th>类型</th><th>位置</th><th>问题描述</th><th>建议</th>
      </tr></thead>
      <tbody>
        {"".join(issues_html_rows)}
      </tbody>
    </table>
  </div>
</div>

<!-- ▸ 重组建议 -->
<div class="section">
  <div class="section-title">文件重组建议 ({len(result.reorg_suggestions)} 条)</div>
  {"<div class='no-issues'>无需重组，文件结构合理。</div>" if not result.reorg_suggestions else ""}
  <div class="reorg-grid">
    {"".join(reorg_html)}
  </div>
</div>

</div><!-- /container -->

<script>
// ── Chart.js 全局配置 ──
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#334155';
Chart.defaults.font.family = "'Segoe UI', system-ui, sans-serif";

const PALETTE = ['#38bdf8','#818cf8','#f87171','#fbbf24','#34d399','#fb923c','#e879f9'];

// 问题分类饼图
new Chart(document.getElementById('chartCategory'), {{
  type: 'doughnut',
  data: {{
    labels: {chart_categories},
    datasets: [{{ data: {chart_values}, backgroundColor: PALETTE, borderWidth: 0 }}]
  }},
  options: {{
    plugins: {{
      legend: {{ position: 'bottom' }}
    }}
  }}
}});

// 函数长度柱状图
new Chart(document.getElementById('chartFuncLen'), {{
  type: 'bar',
  data: {{
    labels: {func_names_chart},
    datasets: [{{
      label: '行数',
      data: {func_lines_chart},
      backgroundColor: (ctx) => ctx.raw > {MAX_FUNC_LINES} ? '#f87171' : '#38bdf8',
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{
      legend: {{ display: false }},
      annotation: {{ annotations: {{ threshold: {{
        type: 'line', xMin: {MAX_FUNC_LINES}, xMax: {MAX_FUNC_LINES},
        borderColor: '#fbbf24', borderWidth: 2, borderDash: [6,3],
        label: {{ content: '阈值', display: true, position: 'end' }}
      }} }} }}
    }},
    scales: {{ x: {{ beginAtZero: true }} }}
  }}
}});

// 圈复杂度柱状图
new Chart(document.getElementById('chartComplexity'), {{
  type: 'bar',
  data: {{
    labels: {complex_names},
    datasets: [{{
      label: '复杂度',
      data: {complex_values},
      backgroundColor: (ctx) => ctx.raw > {MAX_COMPLEXITY} ? '#f87171' : '#818cf8',
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true }} }}
  }}
}});

// 文件大小柱状图
const fileSizes = {file_sizes};
new Chart(document.getElementById('chartFileSize'), {{
  type: 'bar',
  data: {{
    labels: fileSizes.map(f => f.name.length > 30 ? '...' + f.name.slice(-28) : f.name),
    datasets: [{{
      label: '行数',
      data: fileSizes.map(f => f.value),
      backgroundColor: fileSizes.map(f => f.value > {MAX_FILE_LINES} ? '#f87171' : '#34d399'),
      borderRadius: 4,
    }}]
  }},
  options: {{
    indexAxis: 'y',
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true }} }}
  }}
}});

// ── 健康评分 ──
(function() {{
  const total = {len(result.func_infos)} || 1;
  const issues = {len(result.issues)};
  const highIssues = {severity_counts['high']};
  const score = Math.max(0, Math.round(100 - (highIssues * 8 + (issues - highIssues) * 3) * 100 / (total * 10)));
  const el = document.getElementById('healthScore');
  const label = document.getElementById('healthLabel');
  el.textContent = score + ' / 100';
  if (score >= 80) {{ el.style.color = '#34d399'; label.textContent = '优秀 - 代码质量良好'; }}
  else if (score >= 60) {{ el.style.color = '#fbbf24'; label.textContent = '一般 - 建议关注高优问题'; }}
  else {{ el.style.color = '#f87171'; label.textContent = '需改进 - 存在较多质量问题'; }}
}})();

// ── 表格过滤 ──
function filterTable() {{
  const sev = document.getElementById('filterSev').value;
  const cat = document.getElementById('filterCat').value;
  const search = document.getElementById('filterSearch').value.toLowerCase();
  const rows = document.querySelectorAll('#issuesTable tbody tr');
  rows.forEach(row => {{
    const cells = row.querySelectorAll('td');
    const rowSev = cells[0].textContent.trim();
    const rowCat = cells[1].textContent.trim();
    const rowText = row.textContent.toLowerCase();
    let show = true;
    if (sev !== 'all' && rowSev !== sev) show = false;
    if (cat !== 'all' && rowCat !== cat) show = false;
    if (search && !rowText.includes(search)) show = false;
    row.style.display = show ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


# ─── CLI 入口 ────────────────────────────────────────────────────────────────
def main():
    global MAX_FUNC_LINES, MAX_FUNC_PARAMS, MAX_LOCAL_VARS, MAX_COMPLEXITY, MAX_FILE_LINES
    import argparse
    parser = argparse.ArgumentParser(
        description="Python 代码重构审查建议工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              python refactor_analyzer.py .
              python refactor_analyzer.py /path/to/project -o report.html
              python refactor_analyzer.py . --max-func-lines 50 --max-complexity 15
        """))
    parser.add_argument("project_dir", help="要扫描的 Python 项目目录")
    parser.add_argument("-o", "--output", default="refactor_report.html",
                        help="输出 HTML 报告路径 (默认: refactor_report.html)")
    parser.add_argument("--max-func-lines", type=int, default=MAX_FUNC_LINES,
                        help=f"函数最大行数阈值 (默认: {MAX_FUNC_LINES})")
    parser.add_argument("--max-params", type=int, default=MAX_FUNC_PARAMS,
                        help=f"函数最大参数数 (默认: {MAX_FUNC_PARAMS})")
    parser.add_argument("--max-vars", type=int, default=MAX_LOCAL_VARS,
                        help=f"局部变量最大数 (默认: {MAX_LOCAL_VARS})")
    parser.add_argument("--max-complexity", type=int, default=MAX_COMPLEXITY,
                        help=f"圈复杂度阈值 (默认: {MAX_COMPLEXITY})")
    parser.add_argument("--max-file-lines", type=int, default=MAX_FILE_LINES,
                        help=f"文件最大行数 (默认: {MAX_FILE_LINES})")

    args = parser.parse_args()

    # 更新阈值
    MAX_FUNC_LINES = args.max_func_lines
    MAX_FUNC_PARAMS = args.max_params
    MAX_LOCAL_VARS = args.max_vars
    MAX_COMPLEXITY = args.max_complexity
    MAX_FILE_LINES = args.max_file_lines

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"错误: 目录不存在 - {project_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"正在扫描: {project_dir}")
    result = scan_project(project_dir)

    print(f"扫描完成:")
    print(f"  文件数: {result.files_analyzed}")
    print(f"  总行数: {result.total_lines:,}")
    print(f"  函数数: {len(result.func_infos)}")
    print(f"  问题数: {len(result.issues)}")
    print(f"  重组建议: {len(result.reorg_suggestions)} 条")

    output_path = os.path.abspath(args.output)
    generate_html_report(result, output_path)
    print(f"\n报告已生成: {output_path}")


if __name__ == "__main__":
    main()
