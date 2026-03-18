#!/usr/bin/env python3
"""
Python 代码重构审查 MCP Server
基于 FastMCP 实现，提供代码质量扫描、重构建议、HTML 报告生成等工具。

配置示例 (Claude Desktop / VSCode mcp.json):
{
  "mcpServers": {
    "py-refactor": {
      "command": "python",
      "args": ["/path/to/refactor_mcp_server.py"]
    }
  }
}
"""

import ast
import os
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from collections import defaultdict
from pathlib import Path
from mcp.server.fastmcp import FastMCP

# ─── 初始化 MCP Server ─────────────────────────────────────────────────────
mcp = FastMCP("py-refactor")

# ─── 默认阈值 ──────────────────────────────────────────────────────────────
DEFAULT_THRESHOLDS = {
    "max_func_lines": 30,
    "max_func_params": 5,
    "max_local_vars": 8,
    "max_complexity": 10,
    "max_file_lines": 400,
    "max_classes_per_file": 4,
    "max_funcs_per_file": 10,
    "min_similar_prefix": 2,
}

# ─── 自动排除目录 ──────────────────────────────────────────────────────────
EXCLUDE_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs", "env",
}


# ─── 数据结构 ──────────────────────────────────────────────────────────────
@dataclass
class Issue:
    file: str
    line: int
    category: str
    severity: str
    title: str
    detail: str
    suggestion: str


@dataclass
class ReorgSuggestion:
    source_file: str
    items: list
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


# ─── AST 分析器 ────────────────────────────────────────────────────────────
class CodeAnalyzer(ast.NodeVisitor):

    def __init__(self, filepath: str, source: str):
        self.filepath = filepath
        self.source = source
        self.functions: list[FuncInfo] = []
        self.classes: list[str] = []
        self.top_functions: list[str] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            self.imports.append(f"{module}.{alias.name}")
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.classes.append(node.name)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node):
        self._analyze_function(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _analyze_function(self, node):
        is_method = len(self._class_stack) > 0
        class_name = self._class_stack[-1] if is_method else None
        if not is_method:
            self.top_functions.append(node.name)

        end_line = node.end_lineno or node.lineno
        num_lines = end_line - node.lineno + 1

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

        local_vars = self._collect_local_vars(node)
        complexity = self._calc_complexity(node)

        decorators = []
        for d in node.decorator_list:
            if isinstance(d, ast.Name):
                decorators.append(d.id)
            elif isinstance(d, ast.Attribute):
                decorators.append(d.attr)

        self.functions.append(FuncInfo(
            name=node.name, file=self.filepath, line=node.lineno,
            end_line=end_line, num_lines=num_lines, num_params=num_params,
            local_vars=local_vars, complexity=complexity, decorators=decorators,
            is_method=is_method, class_name=class_name,
        ))

    def _collect_local_vars(self, func_node) -> list[str]:
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
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += 1 + len(child.ifs)
        return complexity


# ─── 核心分析引擎 ──────────────────────────────────────────────────────────
def _scan_project(root_dir: str, thresholds: dict) -> AnalysisResult:
    result = AnalysisResult()
    root = Path(root_dir).resolve()

    if not root.is_dir():
        return result

    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
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
            path=rel_path, total_lines=len(lines),
            classes=analyzer.classes, top_functions=analyzer.top_functions,
            imports=analyzer.imports,
        )
        result.file_infos.append(fi)
        result.func_infos.extend(analyzer.functions)

        _check_file_issues(fi, result, thresholds)
        for func in analyzer.functions:
            _check_func_issues(func, result, thresholds)

    _generate_reorg_suggestions(result, thresholds)
    return result


def _check_file_issues(fi: FileInfo, result: AnalysisResult, t: dict):
    if fi.total_lines > t["max_file_lines"]:
        result.issues.append(Issue(
            file=fi.path, line=1, category="long_file", severity="medium",
            title=f"文件过长 ({fi.total_lines} 行)",
            detail=f"文件 {fi.path} 共 {fi.total_lines} 行，超过阈值 {t['max_file_lines']} 行。",
            suggestion="建议按功能拆分为多个模块文件。",
        ))
    if len(fi.classes) > t["max_classes_per_file"]:
        result.issues.append(Issue(
            file=fi.path, line=1, category="too_many_classes", severity="medium",
            title=f"单文件类过多 ({len(fi.classes)} 个)",
            detail=f"文件包含 {len(fi.classes)} 个类: {', '.join(fi.classes)}",
            suggestion="建议每个类（或紧密相关的类）放入独立文件中。",
        ))
    if len(fi.top_functions) > t["max_funcs_per_file"]:
        result.issues.append(Issue(
            file=fi.path, line=1, category="too_many_funcs", severity="low",
            title=f"单文件函数过多 ({len(fi.top_functions)} 个)",
            detail=f"文件包含 {len(fi.top_functions)} 个顶层函数。",
            suggestion="建议按职责将函数分组到不同模块。",
        ))


def _check_func_issues(func: FuncInfo, result: AnalysisResult, t: dict):
    name = f"{func.class_name}.{func.name}" if func.class_name else func.name

    if func.num_lines > t["max_func_lines"]:
        result.issues.append(Issue(
            file=func.file, line=func.line, category="long_func",
            severity="high" if func.num_lines > t["max_func_lines"] * 2 else "medium",
            title=f"函数过长: {name} ({func.num_lines} 行)",
            detail=f"函数 {name} 共 {func.num_lines} 行 (第{func.line}-{func.end_line}行)，阈值 {t['max_func_lines']} 行。",
            suggestion="建议将函数拆分为多个小函数，每个函数只做一件事。",
        ))
    if func.num_params > t["max_func_params"]:
        result.issues.append(Issue(
            file=func.file, line=func.line, category="too_many_params", severity="medium",
            title=f"参数过多: {name} ({func.num_params} 个参数)",
            detail=f"函数 {name} 有 {func.num_params} 个参数，阈值 {t['max_func_params']}。",
            suggestion="建议使用 dataclass 或 TypedDict 封装参数，或拆分函数职责。",
        ))
    if len(func.local_vars) > t["max_local_vars"]:
        result.issues.append(Issue(
            file=func.file, line=func.line, category="extract_vars", severity="medium",
            title=f"局部变量过多: {name} ({len(func.local_vars)} 个变量)",
            detail=f"变量列表: {', '.join(func.local_vars[:15])}{'...' if len(func.local_vars) > 15 else ''}",
            suggestion="建议将相关变量和逻辑提取为独立函数或数据类，降低单函数复杂度。",
        ))
    if func.complexity > t["max_complexity"]:
        result.issues.append(Issue(
            file=func.file, line=func.line, category="high_complexity",
            severity="high" if func.complexity > t["max_complexity"] * 2 else "medium",
            title=f"圈复杂度过高: {name} (复杂度 {func.complexity})",
            detail=f"函数 {name} 的 McCabe 圈复杂度为 {func.complexity}，阈值 {t['max_complexity']}。",
            suggestion="建议使用早返回、策略模式或将条件分支提取为子函数来降低复杂度。",
        ))


def _generate_reorg_suggestions(result: AnalysisResult, t: dict):
    file_funcs: dict[str, list[FuncInfo]] = defaultdict(list)
    for f in result.func_infos:
        if not f.is_method:
            file_funcs[f.file].append(f)

    for filepath, funcs in file_funcs.items():
        if len(funcs) < t["min_similar_prefix"] * 2:
            continue
        prefix_groups: dict[str, list[str]] = defaultdict(list)
        for f in funcs:
            parts = f.name.split("_")
            if len(parts) >= 2 and not f.name.startswith("_"):
                prefix_groups[parts[0]].append(f.name)
        for prefix, names in prefix_groups.items():
            if len(names) >= t["min_similar_prefix"]:
                result.reorg_suggestions.append(ReorgSuggestion(
                    source_file=filepath, items=names,
                    suggested_file=f"{prefix}_utils.py",
                    reason=f"这 {len(names)} 个函数共享前缀 '{prefix}_'，功能可能相关，建议提取到独立模块。",
                ))

    for fi in result.file_infos:
        if len(fi.classes) > t["max_classes_per_file"]:
            for cls_name in fi.classes:
                snake = "".join(f"_{c.lower()}" if c.isupper() and i > 0 else c.lower()
                                for i, c in enumerate(cls_name))
                result.reorg_suggestions.append(ReorgSuggestion(
                    source_file=fi.path, items=[cls_name],
                    suggested_file=f"{snake}.py",
                    reason=f"类 {cls_name} 可独立为单独模块，降低文件复杂度。",
                ))


def _calc_health_score(result: AnalysisResult) -> int:
    total = len(result.func_infos) or 1
    high = sum(1 for i in result.issues if i.severity == "high")
    other = len(result.issues) - high
    return max(0, round(100 - (high * 8 + other * 3) * 100 / (total * 10)))


def _merge_thresholds(**overrides) -> dict:
    t = DEFAULT_THRESHOLDS.copy()
    for k, v in overrides.items():
        if v is not None and k in t:
            t[k] = v
    return t


# ═══════════════════════════════════════════════════════════════════════════
#  MCP Tools
# ═══════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def scan_project(
    project_dir: str,
    max_func_lines: int = 30,
    max_func_params: int = 5,
    max_local_vars: int = 8,
    max_complexity: int = 10,
    max_file_lines: int = 400,
) -> str:
    """Scan a Python project for code quality issues and refactoring opportunities.

    Analyzes all .py files for: overly long functions, too many parameters,
    excessive local variables, high cyclomatic complexity, oversized files,
    and files with too many classes/functions.

    Args:
        project_dir: Absolute path to the Python project directory to scan
        max_func_lines: Max lines per function before flagging (default: 30)
        max_func_params: Max parameters per function before flagging (default: 5)
        max_local_vars: Max local variables per function before flagging (default: 8)
        max_complexity: Max McCabe cyclomatic complexity before flagging (default: 10)
        max_file_lines: Max lines per file before flagging (default: 400)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds(
        max_func_lines=max_func_lines, max_func_params=max_func_params,
        max_local_vars=max_local_vars, max_complexity=max_complexity,
        max_file_lines=max_file_lines,
    )

    result = _scan_project(project_dir, t)
    score = _calc_health_score(result)

    sev_counts = {"high": 0, "medium": 0, "low": 0}
    cat_counts = defaultdict(int)
    for iss in result.issues:
        sev_counts[iss.severity] += 1
        cat_counts[iss.category] += 1

    lines = [
        f"=== Python 代码重构审查报告 ===",
        f"项目路径: {path.resolve()}",
        f"健康评分: {score}/100",
        f"",
        f"--- 统计概览 ---",
        f"扫描文件数: {result.files_analyzed}",
        f"总代码行数: {result.total_lines:,}",
        f"函数/方法数: {len(result.func_infos)}",
        f"问题总数: {len(result.issues)} (高:{sev_counts['high']} 中:{sev_counts['medium']} 低:{sev_counts['low']})",
        f"重组建议数: {len(result.reorg_suggestions)}",
    ]

    if result.issues:
        lines.append(f"\n--- 问题详情 ({len(result.issues)} 个) ---")
        for iss in sorted(result.issues, key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i.severity],)):
            sev_mark = {"high": "[!!!]", "medium": "[!!]", "low": "[!]"}[iss.severity]
            lines.append(f"\n{sev_mark} {iss.title}")
            lines.append(f"  位置: {iss.file}:{iss.line}")
            lines.append(f"  详情: {iss.detail}")
            lines.append(f"  建议: {iss.suggestion}")

    if result.reorg_suggestions:
        lines.append(f"\n--- 文件重组建议 ({len(result.reorg_suggestions)} 条) ---")
        for s in result.reorg_suggestions:
            lines.append(f"\n  {s.source_file} → {s.suggested_file}")
            lines.append(f"    移动项: {', '.join(s.items)}")
            lines.append(f"    原因: {s.reason}")

    return "\n".join(lines)


@mcp.tool()
async def analyze_function(
    file_path: str,
    function_name: str,
) -> str:
    """Analyze a specific function in a Python file for refactoring suggestions.

    Returns detailed metrics: line count, parameter count, local variable list,
    cyclomatic complexity, and specific refactoring recommendations.

    Args:
        file_path: Absolute path to the Python file
        function_name: Name of the function to analyze (use ClassName.method for methods)
    """
    path = Path(file_path)
    if not path.is_file():
        return f"Error: File does not exist: {file_path}"
    if not file_path.endswith(".py"):
        return f"Error: Not a Python file: {file_path}"

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return f"Error: Syntax error in {file_path}: {e}"

    analyzer = CodeAnalyzer(str(path), source)
    analyzer.visit(tree)

    # 支持 ClassName.method_name 格式
    parts = function_name.split(".")
    target_class = parts[0] if len(parts) > 1 else None
    target_func = parts[-1]

    found = None
    for f in analyzer.functions:
        if f.name == target_func:
            if target_class is None or f.class_name == target_class:
                found = f
                break

    if not found:
        available = [f"{f.class_name}.{f.name}" if f.class_name else f.name
                     for f in analyzer.functions]
        return (f"Error: Function '{function_name}' not found in {file_path}\n"
                f"Available functions: {', '.join(available[:30])}")

    f = found
    display = f"{f.class_name}.{f.name}" if f.class_name else f.name
    t = DEFAULT_THRESHOLDS

    lines = [
        f"=== 函数分析: {display} ===",
        f"文件: {file_path}",
        f"位置: 第 {f.line}-{f.end_line} 行",
        f"类型: {'方法' if f.is_method else '函数'}",
        f"",
        f"--- 指标 ---",
        f"行数: {f.num_lines}  (阈值: {t['max_func_lines']}){' ⚠ 超标' if f.num_lines > t['max_func_lines'] else ' ✓'}",
        f"参数数: {f.num_params}  (阈值: {t['max_func_params']}){' ⚠ 超标' if f.num_params > t['max_func_params'] else ' ✓'}",
        f"局部变量数: {len(f.local_vars)}  (阈值: {t['max_local_vars']}){' ⚠ 超标' if len(f.local_vars) > t['max_local_vars'] else ' ✓'}",
        f"圈复杂度: {f.complexity}  (阈值: {t['max_complexity']}){' ⚠ 超标' if f.complexity > t['max_complexity'] else ' ✓'}",
    ]

    if f.local_vars:
        lines.append(f"\n局部变量列表: {', '.join(f.local_vars)}")
    if f.decorators:
        lines.append(f"装饰器: {', '.join(f.decorators)}")

    # 生成具体建议
    suggestions = []
    if f.num_lines > t["max_func_lines"]:
        suggestions.append(
            f"• 函数有 {f.num_lines} 行，建议拆分为多个小函数，每个只做一件事。"
            f"可寻找独立的逻辑块（如验证、转换、持久化）提取为子函数。"
        )
    if f.num_params > t["max_func_params"]:
        suggestions.append(
            f"• 参数过多 ({f.num_params} 个)，建议引入参数对象："
            f"用 @dataclass 将关联参数封装为一个类。"
        )
    if len(f.local_vars) > t["max_local_vars"]:
        suggestions.append(
            f"• 局部变量过多 ({len(f.local_vars)} 个)，说明函数承担了太多职责。"
            f"建议将变量按用途分组，把每组变量及相关逻辑提取为独立函数。"
        )
    if f.complexity > t["max_complexity"]:
        suggestions.append(
            f"• 圈复杂度 {f.complexity}，分支过多。建议：\n"
            f"  - 使用早返回 (guard clause) 减少嵌套\n"
            f"  - 用字典映射替代多重 if-elif\n"
            f"  - 将复杂条件提取为有意义名称的辅助函数"
        )

    if suggestions:
        lines.append(f"\n--- 重构建议 ---")
        lines.extend(suggestions)
    else:
        lines.append(f"\n✓ 该函数各项指标均在阈值范围内，代码质量良好。")

    return "\n".join(lines)


@mcp.tool()
async def analyze_file(file_path: str) -> str:
    """Analyze a single Python file for code quality overview.

    Returns file-level metrics: total lines, class count, function count,
    import count, and a ranked list of all functions by complexity and length.

    Args:
        file_path: Absolute path to the Python file to analyze
    """
    path = Path(file_path)
    if not path.is_file():
        return f"Error: File does not exist: {file_path}"
    if not file_path.endswith(".py"):
        return f"Error: Not a Python file: {file_path}"

    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as e:
        return f"Error: Syntax error in {file_path}: {e}"

    analyzer = CodeAnalyzer(str(path), source)
    analyzer.visit(tree)
    total_lines = len(source.splitlines())
    t = DEFAULT_THRESHOLDS

    lines = [
        f"=== 文件分析: {path.name} ===",
        f"路径: {path.resolve()}",
        f"",
        f"--- 文件级指标 ---",
        f"总行数: {total_lines}  (阈值: {t['max_file_lines']}){' ⚠' if total_lines > t['max_file_lines'] else ' ✓'}",
        f"类数量: {len(analyzer.classes)}{' ⚠' if len(analyzer.classes) > t['max_classes_per_file'] else ' ✓'}",
        f"顶层函数数: {len(analyzer.top_functions)}{' ⚠' if len(analyzer.top_functions) > t['max_funcs_per_file'] else ' ✓'}",
        f"导入数量: {len(analyzer.imports)}",
        f"函数/方法总数: {len(analyzer.functions)}",
    ]

    if analyzer.classes:
        lines.append(f"\n类列表: {', '.join(analyzer.classes)}")

    if analyzer.functions:
        lines.append(f"\n--- 函数/方法排行 ---")
        lines.append(f"{'函数名':<35} {'行数':>5} {'参数':>5} {'变量':>5} {'复杂度':>6}")
        lines.append("-" * 62)

        sorted_funcs = sorted(analyzer.functions, key=lambda f: f.complexity, reverse=True)
        for f in sorted_funcs:
            name = f"{f.class_name}.{f.name}" if f.class_name else f.name
            if len(name) > 34:
                name = name[:31] + "..."
            flags = ""
            if f.num_lines > t["max_func_lines"]:
                flags += "L"
            if f.num_params > t["max_func_params"]:
                flags += "P"
            if len(f.local_vars) > t["max_local_vars"]:
                flags += "V"
            if f.complexity > t["max_complexity"]:
                flags += "C"
            flag_str = f" [{flags}]" if flags else ""
            lines.append(f"{name:<35} {f.num_lines:>5} {f.num_params:>5} {len(f.local_vars):>5} {f.complexity:>6}{flag_str}")

        lines.append(f"\n标记说明: L=行数超标 P=参数过多 V=变量过多 C=复杂度高")

    return "\n".join(lines)


@mcp.tool()
async def find_long_functions(
    project_dir: str,
    min_lines: int = 30,
    top_n: int = 20,
) -> str:
    """Find the longest functions across a Python project.

    Scans all .py files and returns a ranked list of functions exceeding
    the minimum line threshold, sorted by length (longest first).

    Args:
        project_dir: Absolute path to the Python project directory
        min_lines: Minimum function lines to include in results (default: 30)
        top_n: Maximum number of results to return (default: 20)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds(max_func_lines=min_lines)
    result = _scan_project(project_dir, t)

    all_funcs = sorted(result.func_infos, key=lambda f: f.num_lines, reverse=True)
    long_funcs = [f for f in all_funcs if f.num_lines >= min_lines][:top_n]

    if not long_funcs:
        return f"未发现超过 {min_lines} 行的函数。项目共 {len(all_funcs)} 个函数，代码简洁！"

    lines = [
        f"=== 超长函数排行 TOP {len(long_funcs)} (阈值: {min_lines} 行) ===",
        f"项目: {path.resolve()}",
        f"",
        f"{'#':>3} {'函数名':<40} {'行数':>6} {'文件位置'}",
        "-" * 85,
    ]

    for i, f in enumerate(long_funcs, 1):
        name = f"{f.class_name}.{f.name}" if f.class_name else f.name
        if len(name) > 39:
            name = name[:36] + "..."
        lines.append(f"{i:>3} {name:<40} {f.num_lines:>6} {f.file}:{f.line}")

    return "\n".join(lines)


@mcp.tool()
async def find_complex_functions(
    project_dir: str,
    min_complexity: int = 10,
    top_n: int = 20,
) -> str:
    """Find functions with highest cyclomatic complexity in a Python project.

    Scans all .py files and returns a ranked list of functions exceeding
    the minimum complexity threshold, sorted by complexity (highest first).

    Args:
        project_dir: Absolute path to the Python project directory
        min_complexity: Minimum McCabe complexity to include (default: 10)
        top_n: Maximum number of results to return (default: 20)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds(max_complexity=min_complexity)
    result = _scan_project(project_dir, t)

    all_funcs = sorted(result.func_infos, key=lambda f: f.complexity, reverse=True)
    complex_funcs = [f for f in all_funcs if f.complexity >= min_complexity][:top_n]

    if not complex_funcs:
        return f"未发现复杂度超过 {min_complexity} 的函数。项目共 {len(all_funcs)} 个函数，逻辑清晰！"

    lines = [
        f"=== 高复杂度函数排行 TOP {len(complex_funcs)} (阈值: {min_complexity}) ===",
        f"项目: {path.resolve()}",
        f"",
        f"{'#':>3} {'函数名':<40} {'复杂度':>6} {'行数':>6} {'文件位置'}",
        "-" * 90,
    ]

    for i, f in enumerate(complex_funcs, 1):
        name = f"{f.class_name}.{f.name}" if f.class_name else f.name
        if len(name) > 39:
            name = name[:36] + "..."
        lines.append(f"{i:>3} {name:<40} {f.complexity:>6} {f.num_lines:>6} {f.file}:{f.line}")

    return "\n".join(lines)


@mcp.tool()
async def suggest_file_reorg(project_dir: str) -> str:
    """Suggest how to reorganize files in a Python project.

    Analyzes function naming patterns and class distribution to suggest
    which functions/classes should be moved to new or different files.

    Args:
        project_dir: Absolute path to the Python project directory
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds()
    result = _scan_project(project_dir, t)

    if not result.reorg_suggestions:
        return (f"项目 {path.resolve()} 文件结构合理，暂无重组建议。\n"
                f"(扫描了 {result.files_analyzed} 个文件，{len(result.func_infos)} 个函数)")

    lines = [
        f"=== 文件重组建议 ===",
        f"项目: {path.resolve()}",
        f"建议数: {len(result.reorg_suggestions)}",
        f"",
    ]

    for i, s in enumerate(result.reorg_suggestions, 1):
        lines.append(f"[{i}] {s.source_file}  →  {s.suggested_file}")
        lines.append(f"    移动项: {', '.join(s.items)}")
        lines.append(f"    原因: {s.reason}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def generate_report(
    project_dir: str,
    output_path: str = "refactor_report.html",
    max_func_lines: int = 30,
    max_complexity: int = 10,
) -> str:
    """Generate an HTML visual report for Python project code quality.

    Creates a comprehensive HTML report with: health score, issue distribution
    charts, function length/complexity rankings, issue detail table with
    filtering, and file reorganization suggestions.

    Args:
        project_dir: Absolute path to the Python project directory to scan
        output_path: Path for the output HTML report file (default: refactor_report.html)
        max_func_lines: Max lines per function threshold (default: 30)
        max_complexity: Max McCabe complexity threshold (default: 10)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds(max_func_lines=max_func_lines, max_complexity=max_complexity)
    result = _scan_project(project_dir, t)

    # 复用 refactor_analyzer 的 HTML 生成逻辑
    try:
        html = _build_html_report(result, t)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        return (f"HTML 报告已生成: {out.resolve()}\n"
                f"扫描: {result.files_analyzed} 文件, {result.total_lines:,} 行, "
                f"{len(result.issues)} 个问题, 健康评分: {_calc_health_score(result)}/100")
    except Exception as e:
        return f"Error generating report: {e}"


@mcp.tool()
async def health_score(project_dir: str) -> str:
    """Get a quick health score (0-100) for a Python project.

    Performs a fast scan and returns the overall quality score with a brief
    summary of issue counts by severity.

    Args:
        project_dir: Absolute path to the Python project directory
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    t = _merge_thresholds()
    result = _scan_project(project_dir, t)
    score = _calc_health_score(result)

    sev = {"high": 0, "medium": 0, "low": 0}
    for iss in result.issues:
        sev[iss.severity] += 1

    if score >= 80:
        grade, comment = "A", "优秀 - 代码质量良好"
    elif score >= 60:
        grade, comment = "B", "一般 - 建议关注高优问题"
    elif score >= 40:
        grade, comment = "C", "需改进 - 存在较多质量问题"
    else:
        grade, comment = "D", "较差 - 强烈建议重构"

    return (f"项目健康评分: {score}/100 (等级: {grade})\n"
            f"评价: {comment}\n"
            f"\n"
            f"文件数: {result.files_analyzed} | 代码行: {result.total_lines:,} | 函数数: {len(result.func_infos)}\n"
            f"问题: 高严重 {sev['high']} | 中严重 {sev['medium']} | 低严重 {sev['low']}\n"
            f"重组建议: {len(result.reorg_suggestions)} 条")


@mcp.tool()
async def auto_refactor(
    project_dir: str,
    apply: bool = False,
    backup: bool = True,
    file_only: bool = False,
    func_only: bool = False,
    max_func_lines: int = 30,
    max_file_lines: int = 400,
) -> str:
    """Automatically refactor a Python project: split long functions, split large files, and reorganize by category.

    In preview mode (apply=False), returns the refactoring plan without modifying any files.
    In apply mode (apply=True), executes the refactoring and modifies/creates files.

    Args:
        project_dir: Absolute path to the Python project directory
        apply: Whether to actually execute the refactoring (default: False, preview only)
        backup: Whether to backup original files as .bak before modifying (default: True)
        file_only: Only perform file splitting, skip function splitting (default: False)
        func_only: Only perform function splitting, skip file splitting (default: False)
        max_func_lines: Functions longer than this will be split (default: 30)
        max_file_lines: Files longer than this will be split (default: 400)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    try:
        from refactor_auto import (
            analyze_project, apply_file_split, apply_func_split,
            MAX_FUNC_LINES as _mfl, MAX_FILE_LINES as _mfll,
        )
        import refactor_auto
        refactor_auto.MAX_FUNC_LINES = max_func_lines
        refactor_auto.MAX_FILE_LINES = max_file_lines
    except ImportError:
        return ("Error: refactor_auto.py not found. "
                "Please ensure it is in the same directory as refactor_mcp_server.py")

    actions = analyze_project(project_dir)

    if file_only:
        actions = [a for a in actions if a.kind == "split_file"]
    elif func_only:
        actions = [a for a in actions if a.kind == "split_func"]

    if not actions:
        return "未发现需要重构的内容，代码结构良好！"

    func_splits = [a for a in actions if a.kind == "split_func"]
    file_splits = [a for a in actions if a.kind == "split_file"]

    lines = [
        f"=== 自动重构{'执行报告' if apply else '计划预览'} ===",
        f"项目: {path.resolve()}",
        f"函数拆分: {len(func_splits)} 个",
        f"文件拆分: {len(file_splits)} 个",
        f"总操作数: {len(actions)} 个",
    ]

    if func_splits:
        lines.append(f"\n--- 函数拆分 ---")
        for a in func_splits:
            lines.append(f"\n{a.description}")
            for d in a.details:
                lines.append(f"  {d}")

    if file_splits:
        lines.append(f"\n--- 文件拆分 ---")
        for a in file_splits:
            lines.append(f"\n{a.description}")
            for d in a.details:
                lines.append(f"  {d}")

    if apply:
        lines.append(f"\n--- 执行结果 ---")
        for a in file_splits:
            try:
                apply_file_split(a, project_dir, backup=backup)
                lines.append(f"  [OK] {a.description}")
            except Exception as e:
                lines.append(f"  [FAIL] {a.description}: {e}")

        for a in func_splits:
            try:
                apply_func_split(a, project_dir, backup=backup)
                lines.append(f"  [OK] {a.description}")
            except Exception as e:
                lines.append(f"  [FAIL] {a.description}: {e}")

        lines.append(f"\n重构完成！" + (" 原文件已备份为 .bak" if backup else ""))
    else:
        lines.append(f"\n提示: 设置 apply=True 执行重构")

    return "\n".join(lines)


# ─── ydiff: structural code diff ──────────────────────────────────────────

@mcp.tool()
async def ydiff_files(
    file_path1: str,
    file_path2: str,
    output_path: str = "",
) -> str:
    """Compare two Python files using structural AST-level diff.

    Unlike line-based diff, this understands code structure — it detects moved
    functions, renamed variables, and semantic changes. Generates an interactive
    side-by-side HTML report with click-to-navigate highlighting.

    Args:
        file_path1: Absolute path to the old Python file
        file_path2: Absolute path to the new Python file
        output_path: Output HTML path (default: auto-generated from filenames)
    """
    for fp in (file_path1, file_path2):
        if not Path(fp).is_file():
            return f"Error: File not found: {fp}"

    try:
        from ydiff_python import diff_python, base_name
    except ImportError:
        return ("Error: ydiff_python.py not found. "
                "Please ensure it is in the same directory as refactor_mcp_server.py")

    try:
        if output_path:
            import ydiff_python
            with open(file_path1, 'r', encoding='utf-8') as f:
                text1 = f.read()
            with open(file_path2, 'r', encoding='utf-8') as f:
                text2 = f.read()
            node1 = ydiff_python.parse_python(text1)
            node2 = ydiff_python.parse_python(text2)
            changes = ydiff_python.diff(node1, node2)
            # Temporarily override output
            out = ydiff_python.htmlize(changes, file_path1, file_path2, text1, text2)
            if output_path != out:
                Path(out).rename(output_path)
                out = output_path
        else:
            out = diff_python(file_path1, file_path2)

        return (f"Structural diff report generated: {out}\n"
                f"Open in browser to view interactive side-by-side comparison.\n"
                f"  - Red highlights: deleted code\n"
                f"  - Green highlights: inserted code\n"
                f"  - Gray links: matched/moved code (click to navigate)")
    except SyntaxError as e:
        return f"Error: Failed to parse Python file: {e}"
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
async def ydiff_commit(
    project_dir: str,
    commit_id: str,
    output_path: str = "",
) -> str:
    """Generate a structural diff report for a git commit.

    Analyzes all Python files changed in the specified commit using AST-level
    structural comparison. Produces a multi-file HTML report with a file
    navigator sidebar, left-red/right-green diff panels, and interactive
    click-to-navigate code matching.

    Args:
        project_dir: Absolute path to the git repository
        commit_id: Git commit hash (full or short) to analyze
        output_path: Output HTML path (default: commit-<short_hash>.html)
    """
    path = Path(project_dir)
    if not path.is_dir():
        return f"Error: Directory does not exist: {project_dir}"

    try:
        from ydiff_python import diff_commit
    except ImportError:
        return ("Error: ydiff_python.py not found. "
                "Please ensure it is in the same directory as refactor_mcp_server.py")

    try:
        out = diff_commit(project_dir, commit_id, output_path or None)

        return (f"Commit diff report generated: {out}\n"
                f"Open in browser to view the structural diff.\n"
                f"Features:\n"
                f"  - File navigator sidebar with change status (M/A/D/R)\n"
                f"  - Left panel (red): old version with deletions highlighted\n"
                f"  - Right panel (green): new version with insertions highlighted\n"
                f"  - Click matched code to scroll to corresponding position")
    except RuntimeError as e:
        return f"Git error: {e}"
    except Exception as e:
        return f"Error: {e}"


# ─── HTML 报告生成 ─────────────────────────────────────────────────────────
def _build_html_report(result: AnalysisResult, t: dict) -> str:
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    category_counts = defaultdict(int)
    for iss in result.issues:
        severity_counts[iss.severity] += 1
        category_counts[iss.category] += 1

    top_long = sorted(result.func_infos, key=lambda f: f.num_lines, reverse=True)[:15]
    top_complex = sorted(result.func_infos, key=lambda f: f.complexity, reverse=True)[:15]

    cat_labels = {
        "long_func": "函数过长", "too_many_params": "参数过多",
        "extract_vars": "变量过多", "high_complexity": "复杂度高",
        "long_file": "文件过长", "too_many_classes": "类过多",
        "too_many_funcs": "函数过多",
    }

    chart_cats = json.dumps([cat_labels.get(k, k) for k in category_counts], ensure_ascii=False)
    chart_vals = json.dumps(list(category_counts.values()))
    func_names = json.dumps([f"{f.class_name}.{f.name}" if f.class_name else f.name for f in top_long], ensure_ascii=False)
    func_lines = json.dumps([f.num_lines for f in top_long])
    cx_names = json.dumps([f"{f.class_name}.{f.name}" if f.class_name else f.name for f in top_complex], ensure_ascii=False)
    cx_vals = json.dumps([f.complexity for f in top_complex])
    file_sizes = json.dumps(
        [{"name": fi.path, "value": fi.total_lines}
         for fi in sorted(result.file_infos, key=lambda x: x.total_lines, reverse=True)[:30]],
        ensure_ascii=False)

    issue_rows = []
    for iss in sorted(result.issues, key=lambda i: ({"high": 0, "medium": 1, "low": 2}[i.severity],)):
        sc = {"high": "sev-high", "medium": "sev-med", "low": "sev-low"}[iss.severity]
        sl = {"high": "高", "medium": "中", "low": "低"}[iss.severity]
        cl = cat_labels.get(iss.category, iss.category)
        issue_rows.append(
            f'<tr><td><span class="badge {sc}">{sl}</span></td>'
            f'<td><span class="badge badge-cat">{cl}</span></td>'
            f'<td class="fc">{iss.file}:{iss.line}</td>'
            f'<td><strong>{iss.title}</strong><br><small class="tm">{iss.detail}</small></td>'
            f'<td class="sg">{iss.suggestion}</td></tr>'
        )

    reorg_cards = []
    for s in result.reorg_suggestions:
        reorg_cards.append(
            f'<div class="rc"><div class="rh">'
            f'<span class="rf">{s.source_file}</span><span class="ra">→</span>'
            f'<span class="rt">{s.suggested_file}</span></div>'
            f'<div class="ri">移动: {", ".join(s.items)}</div>'
            f'<div class="rr">{s.reason}</div></div>'
        )

    mfl = t["max_func_lines"]
    mc = t["max_complexity"]
    mfll = t["max_file_lines"]

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Python 代码重构审查报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root{{--bg:#0f172a;--s:#1e293b;--s2:#334155;--bd:#475569;--t:#e2e8f0;--tm:#94a3b8;--a:#38bdf8;--a2:#818cf8;--d:#f87171;--w:#fbbf24;--g:#34d399}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--t);line-height:1.6}}
.ct{{max-width:1400px;margin:0 auto;padding:20px}}
.hd{{text-align:center;padding:40px 20px;background:linear-gradient(135deg,#1e293b,#0f172a);border-bottom:1px solid var(--bd);margin-bottom:30px}}
.hd h1{{font-size:2.2em;background:linear-gradient(135deg,var(--a),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}}
.hd .sub{{color:var(--tm);font-size:1.1em}}
.sb{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:30px}}
.sc{{background:var(--s);border:1px solid var(--bd);border-radius:12px;padding:20px;text-align:center}}
.sn{{font-size:2.4em;font-weight:700;line-height:1.2}}.sl{{color:var(--tm);font-size:.9em;margin-top:4px}}
.td{{color:var(--d)}}.tw{{color:var(--w)}}.tg{{color:var(--g)}}.ta{{color:var(--a)}}
.sec{{background:var(--s);border:1px solid var(--bd);border-radius:12px;margin-bottom:24px;overflow:hidden}}
.st{{padding:16px 24px;font-size:1.3em;font-weight:600;border-bottom:1px solid var(--bd);background:var(--s2)}}
.cg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:24px;margin-bottom:24px}}
.cb{{background:var(--s);border:1px solid var(--bd);border-radius:12px;padding:20px}}
.cb h3{{margin-bottom:16px;font-size:1.1em;color:var(--a)}}.cb canvas{{max-height:350px}}
table{{width:100%;border-collapse:collapse}}
th{{background:var(--s2);padding:12px 16px;text-align:left;font-weight:600;font-size:.85em;text-transform:uppercase;letter-spacing:.5px;color:var(--tm);position:sticky;top:0}}
td{{padding:12px 16px;border-top:1px solid var(--bd);vertical-align:top;font-size:.92em}}
tr:hover{{background:rgba(56,189,248,.05)}}
.fc{{font-family:'Fira Code',monospace;font-size:.85em;color:var(--a);white-space:nowrap}}.sg{{color:var(--g)}}.tm{{color:var(--tm)}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:.8em;font-weight:600;white-space:nowrap}}
.sev-high{{background:rgba(248,113,113,.2);color:var(--d);border:1px solid var(--d)}}
.sev-med{{background:rgba(251,191,36,.2);color:var(--w);border:1px solid var(--w)}}
.sev-low{{background:rgba(52,211,153,.2);color:var(--g);border:1px solid var(--g)}}
.badge-cat{{background:rgba(129,140,248,.15);color:var(--a2);border:1px solid var(--a2)}}
.rg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:16px;padding:20px}}
.rc{{background:var(--bg);border:1px solid var(--bd);border-radius:10px;padding:16px}}
.rh{{display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap}}
.rf{{font-family:monospace;color:var(--d)}}.ra{{color:var(--a);font-size:1.4em}}.rt{{font-family:monospace;color:var(--g);font-weight:600}}
.ri{{font-size:.88em;color:var(--tm);margin-bottom:6px}}.rr{{font-size:.9em;color:var(--a2)}}
.fb{{padding:16px 24px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--bd)}}
.fb label{{color:var(--tm);font-size:.9em}}
.fb select,.fb input{{background:var(--bg);color:var(--t);border:1px solid var(--bd);border-radius:6px;padding:6px 12px;font-size:.9em}}
.ni{{text-align:center;padding:60px 20px;color:var(--g);font-size:1.3em}}
.tw2{{max-height:600px;overflow-y:auto}}
.hs{{font-size:3em;font-weight:700;text-align:center;padding-top:10px}}.hl{{text-align:center;color:var(--tm);font-size:1.1em}}
@media(max-width:768px){{.cg{{grid-template-columns:1fr}}.sb{{grid-template-columns:repeat(2,1fr)}}.rg{{grid-template-columns:1fr}}.ct{{padding:10px}}}}
</style></head><body>
<div class="hd"><h1>Python 代码重构审查报告</h1><p class="sub">自动扫描分析 · 智能重构建议 · 可视化展示</p></div>
<div class="ct">
<div class="sb">
<div class="sc"><div class="sn ta">{result.files_analyzed}</div><div class="sl">扫描文件数</div></div>
<div class="sc"><div class="sn ta">{result.total_lines:,}</div><div class="sl">总代码行数</div></div>
<div class="sc"><div class="sn ta">{len(result.func_infos)}</div><div class="sl">函数/方法数</div></div>
<div class="sc"><div class="sn td">{severity_counts['high']}</div><div class="sl">高严重度</div></div>
<div class="sc"><div class="sn tw">{severity_counts['medium']}</div><div class="sl">中严重度</div></div>
<div class="sc"><div class="sn tg">{severity_counts['low']}</div><div class="sl">低严重度</div></div>
</div>
<div class="sec"><div class="st">项目健康评分</div><div style="padding:20px;text-align:center"><div class="hs" id="hs">--</div><div class="hl" id="hl">计算中...</div></div></div>
<div class="cg">
<div class="cb"><h3>问题分类分布</h3><canvas id="c1"></canvas></div>
<div class="cb"><h3>函数长度 TOP 15</h3><canvas id="c2"></canvas></div>
<div class="cb"><h3>圈复杂度 TOP 15</h3><canvas id="c3"></canvas></div>
<div class="cb"><h3>文件大小分布</h3><canvas id="c4"></canvas></div>
</div>
<div class="sec"><div class="st">问题详情 ({len(result.issues)} 个)</div>
<div class="fb"><label>严重度:</label><select id="fs" onchange="ft()"><option value="all">全部</option><option value="高">高</option><option value="中">中</option><option value="低">低</option></select>
<label>类型:</label><select id="fc" onchange="ft()"><option value="all">全部</option>{"".join(f'<option value="{v}">{v}</option>' for v in cat_labels.values())}</select>
<label>搜索:</label><input id="fi" placeholder="文件名或函数名..." oninput="ft()"></div>
{"<div class='ni'>没有发现问题，代码质量良好！</div>" if not result.issues else ""}
<div class="tw2"><table id="it"><thead><tr><th>严重度</th><th>类型</th><th>位置</th><th>问题描述</th><th>建议</th></tr></thead>
<tbody>{"".join(issue_rows)}</tbody></table></div></div>
<div class="sec"><div class="st">文件重组建议 ({len(result.reorg_suggestions)} 条)</div>
{"<div class='ni'>无需重组，文件结构合理。</div>" if not result.reorg_suggestions else ""}
<div class="rg">{"".join(reorg_cards)}</div></div>
</div>
<script>
Chart.defaults.color='#94a3b8';Chart.defaults.borderColor='#334155';
const P=['#38bdf8','#818cf8','#f87171','#fbbf24','#34d399','#fb923c','#e879f9'];
new Chart(document.getElementById('c1'),{{type:'doughnut',data:{{labels:{chart_cats},datasets:[{{data:{chart_vals},backgroundColor:P,borderWidth:0}}]}},options:{{plugins:{{legend:{{position:'bottom'}}}}}}}});
new Chart(document.getElementById('c2'),{{type:'bar',data:{{labels:{func_names},datasets:[{{label:'行数',data:{func_lines},backgroundColor:c=>c.raw>{mfl}?'#f87171':'#38bdf8',borderRadius:4}}]}},options:{{indexAxis:'y',plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true}}}}}}}});
new Chart(document.getElementById('c3'),{{type:'bar',data:{{labels:{cx_names},datasets:[{{label:'复杂度',data:{cx_vals},backgroundColor:c=>c.raw>{mc}?'#f87171':'#818cf8',borderRadius:4}}]}},options:{{indexAxis:'y',plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true}}}}}}}});
const fs={file_sizes};
new Chart(document.getElementById('c4'),{{type:'bar',data:{{labels:fs.map(f=>f.name.length>30?'...'+f.name.slice(-28):f.name),datasets:[{{label:'行数',data:fs.map(f=>f.value),backgroundColor:fs.map(f=>f.value>{mfll}?'#f87171':'#34d399'),borderRadius:4}}]}},options:{{indexAxis:'y',plugins:{{legend:{{display:false}}}},scales:{{x:{{beginAtZero:true}}}}}}}});
(function(){{const t={len(result.func_infos)}||1,i={len(result.issues)},h={severity_counts['high']};const s=Math.max(0,Math.round(100-(h*8+(i-h)*3)*100/(t*10)));const e=document.getElementById('hs'),l=document.getElementById('hl');e.textContent=s+' / 100';if(s>=80){{e.style.color='#34d399';l.textContent='优秀 - 代码质量良好'}}else if(s>=60){{e.style.color='#fbbf24';l.textContent='一般 - 建议关注高优问题'}}else{{e.style.color='#f87171';l.textContent='需改进 - 存在较多质量问题'}}}})();
function ft(){{const s=document.getElementById('fs').value,c=document.getElementById('fc').value,q=document.getElementById('fi').value.toLowerCase();document.querySelectorAll('#it tbody tr').forEach(r=>{{const d=r.querySelectorAll('td'),sv=d[0].textContent.trim(),ct=d[1].textContent.trim(),tx=r.textContent.toLowerCase();let v=true;if(s!=='all'&&sv!==s)v=false;if(c!=='all'&&ct!==c)v=false;if(q&&!tx.includes(q))v=false;r.style.display=v?'':'none'}})}}
</script></body></html>"""


# ─── 入口 ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport='stdio')
