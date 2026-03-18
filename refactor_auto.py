#!/usr/bin/env python3
"""
Python 自动重构工具
自动拆分过长函数、过长文件，并按分类创建新模块。

策略：
  1. 长函数拆分：基于 AST 分析函数体中的逻辑块，按 if/elif 分支、循环体、
     连续赋值组等边界提取子函数，自动追踪变量依赖。
  2. 文件拆分：将类和按前缀分组的函数移到独立模块，原文件保留 import 重导出。
  3. 分类建档：按函数名前缀自动聚类，生成合理的模块文件。

用法：
  python refactor_auto.py <project_dir>               # 预览模式 (dry-run)
  python refactor_auto.py <project_dir> --apply        # 执行重构
  python refactor_auto.py <project_dir> --apply --backup  # 执行并备份原文件
"""

import ast
import os
import sys
import shutil
import copy
import textwrap
import argparse
import subprocess
from dataclasses import dataclass, field
from collections import defaultdict
from pathlib import Path
from typing import Optional


# ─── 配置 ──────────────────────────────────────────────────────────────────
MAX_FUNC_LINES = 30
MAX_FILE_LINES = 400
MAX_CLASSES_PER_FILE = 4
MAX_FUNCS_PER_FILE = 10
MIN_GROUP_SIZE = 2

EXCLUDE_DIRS = {
    ".venv", "venv", "env", "__pycache__", ".git", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs",
}


# ═══════════════════════════════════════════════════════════════════════════
#  第一部分：变量依赖分析
# ═══════════════════════════════════════════════════════════════════════════

class _NameCollector(ast.NodeVisitor):
    """收集 AST 节点中读取和写入的变量名。"""

    def __init__(self):
        self.reads: set[str] = set()
        self.writes: set[str] = set()

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            self.writes.add(node.id)
        elif isinstance(node.ctx, (ast.Load, ast.Del)):
            self.reads.add(node.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        # 不深入嵌套函数
        pass

    visit_AsyncFunctionDef = visit_FunctionDef


def _collect_names(nodes: list[ast.stmt]) -> tuple[set[str], set[str]]:
    """返回一组语句的 (读取变量集, 写入变量集)。"""
    c = _NameCollector()
    for n in nodes:
        c.visit(n)
    return c.reads, c.writes


def _get_all_names_in_node(node) -> tuple[set[str], set[str]]:
    """单个节点的读/写变量。"""
    c = _NameCollector()
    c.visit(node)
    return c.reads, c.writes


# ═══════════════════════════════════════════════════════════════════════════
#  第二部分：长函数自动拆分
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ExtractedBlock:
    """从长函数中提取出的代码块。"""
    name: str                    # 生成的子函数名
    stmts: list[ast.stmt]       # 原始语句列表
    params: list[str]            # 需要传入的参数
    returns: list[str]           # 需要返回的变量
    start_line: int
    end_line: int
    is_method: bool = False


@dataclass
class FuncSplitPlan:
    """一个函数的拆分计划。"""
    file_path: str
    func_name: str
    class_name: Optional[str]
    original_lines: tuple[int, int]    # (start, end) 行号
    blocks: list[ExtractedBlock]


def _segment_function_body(body: list[ast.stmt], threshold: int = 8) -> list[list[ast.stmt]]:
    """
    将函数体按逻辑边界分段。
    分段策略：
      - 顶层 if/elif/else 分支各为一段
      - 顶层 for/while 循环为一段
      - 顶层 try/except 为一段
      - 连续简单语句（赋值、表达式、return）攒够 threshold 行或遇到控制流时切一段
    """
    segments: list[list[ast.stmt]] = []
    current: list[ast.stmt] = []

    def flush():
        nonlocal current
        if current:
            segments.append(current)
            current = []

    for stmt in body:
        if isinstance(stmt, (ast.If, ast.For, ast.While, ast.AsyncFor,
                             ast.With, ast.AsyncWith, ast.Try)):
            flush()
            segments.append([stmt])
        else:
            current.append(stmt)
            # 简单语句攒够一组就切
            total = sum((getattr(s, 'end_lineno', s.lineno) or s.lineno) - s.lineno + 1
                        for s in current)
            if total >= threshold:
                flush()

    flush()
    return segments


def _plan_function_split(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    file_path: str,
    class_name: Optional[str] = None,
    max_lines: int = MAX_FUNC_LINES,
) -> Optional[FuncSplitPlan]:
    """
    分析一个长函数，生成拆分计划。
    只有当函数体确实可以被拆成有意义的块时才返回计划。
    """
    end_line = func_node.end_lineno or func_node.lineno
    num_lines = end_line - func_node.lineno + 1
    if num_lines <= max_lines:
        return None

    body = func_node.body
    # 跳过只有 docstring 的情况
    real_body = body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, (ast.Constant, ast.Str))):
        real_body = body[1:]

    if len(real_body) < 3:
        return None

    segments = _segment_function_body(real_body)

    # 至少要分出 2 个有意义的段
    meaningful = [s for s in segments if _segment_line_count(s) >= 3]
    if len(meaningful) < 2:
        return None

    # 追踪变量依赖，决定哪些段可以提取
    is_method = class_name is not None
    func_name = func_node.name
    base_name = f"_{func_name}" if not func_name.startswith("_") else func_name

    # 收集函数参数名（作为已定义变量）
    arg_names = set()
    for a in func_node.args.args + func_node.args.posonlyargs + func_node.args.kwonlyargs:
        arg_names.add(a.arg)
    if func_node.args.vararg:
        arg_names.add(func_node.args.vararg.arg)
    if func_node.args.kwarg:
        arg_names.add(func_node.args.kwarg.arg)

    blocks: list[ExtractedBlock] = []
    defined_so_far = set(arg_names)     # 到当前位置已定义的变量
    block_counter = 0

    # 收集整个函数后续所有段用到的变量 (用于判断 return)
    all_future_reads: list[set[str]] = []
    cumulative = set()
    for seg in reversed(segments):
        r, _ = _collect_names(seg)
        cumulative = cumulative | r
        all_future_reads.insert(0, set(cumulative))

    for i, seg in enumerate(segments):
        seg_lines = _segment_line_count(seg)
        if seg_lines < 3:
            # 太短的段不值得提取，但要更新 defined_so_far
            _, w = _collect_names(seg)
            defined_so_far |= w
            continue

        reads, writes = _collect_names(seg)

        # 这个段需要哪些外部变量作为参数
        params = sorted(reads & defined_so_far - {"self", "cls"})

        # 这个段写了哪些变量，后续段还会用到 → 需要 return
        future_reads = all_future_reads[i + 1] if i + 1 < len(segments) else set()
        returns = sorted(writes & future_reads - {"self"})

        # 段中是否包含 return → 不适合提取（会改变控制流）
        has_return = any(
            isinstance(n, ast.Return)
            for s in seg
            for n in ast.walk(s)
        )

        # 包含 return 的段如果是最后一段可以提取，否则跳过
        if has_return and i < len(segments) - 1:
            defined_so_far |= writes
            continue

        block_counter += 1
        start = seg[0].lineno
        end = seg[-1].end_lineno or seg[-1].lineno

        # 给子函数起名
        # 尝试从第一个语句推断用途
        sub_name = _infer_block_name(seg, base_name, block_counter)

        blocks.append(ExtractedBlock(
            name=sub_name,
            stmts=seg,
            params=params,
            returns=returns,
            start_line=start,
            end_line=end,
            is_method=is_method,
        ))

        defined_so_far |= writes

    if len(blocks) < 1:
        return None

    return FuncSplitPlan(
        file_path=file_path,
        func_name=func_name,
        class_name=class_name,
        original_lines=(func_node.lineno, end_line),
        blocks=blocks,
    )


def _segment_line_count(stmts: list[ast.stmt]) -> int:
    if not stmts:
        return 0
    start = stmts[0].lineno
    end = stmts[-1].end_lineno or stmts[-1].lineno
    return end - start + 1


def _infer_block_name(stmts: list[ast.stmt], base: str, idx: int) -> str:
    """尝试从代码块内容推断一个有意义的名字。"""
    first = stmts[0]

    if isinstance(first, ast.If):
        # 从 if 条件推断
        test = first.test
        if isinstance(test, ast.Compare):
            if isinstance(test.left, ast.Name):
                return f"{base}_check_{test.left.id}"
            if isinstance(test.left, ast.Attribute):
                return f"{base}_check_{test.left.attr}"
        if isinstance(test, ast.UnaryOp) and isinstance(test.operand, ast.Name):
            return f"{base}_check_{test.operand.id}"
        if isinstance(test, ast.Name):
            return f"{base}_check_{test.id}"

    if isinstance(first, (ast.For, ast.AsyncFor)):
        if isinstance(first.iter, ast.Name):
            return f"{base}_process_{first.iter.id}"
        if isinstance(first.iter, ast.Attribute):
            return f"{base}_process_{first.iter.attr}"

    if isinstance(first, (ast.Assign, ast.AnnAssign)):
        target = None
        if isinstance(first, ast.Assign) and first.targets:
            t = first.targets[0]
            if isinstance(t, ast.Name):
                target = t.id
        elif isinstance(first, ast.AnnAssign) and isinstance(first.target, ast.Name):
            target = first.target.id
        if target:
            return f"{base}_init_{target}"

    return f"{base}_part{idx}"


def _generate_split_code(
    source_lines: list[str],
    func_node: ast.FunctionDef,
    plan: FuncSplitPlan,
) -> str:
    """
    根据拆分计划，生成重构后的代码文本。
    返回：替换原函数定义区域的新代码（包含子函数 + 重写后的主函数）。
    """
    indent = _detect_indent(source_lines, func_node)
    inner_indent = indent + "    "
    is_method = plan.class_name is not None

    output_parts = []

    # 1) 生成提取出的子函数
    for block in plan.blocks:
        output_parts.append("")  # 空行分隔

        # 函数签名
        if is_method:
            params_str = "self" + (", " + ", ".join(block.params) if block.params else "")
        else:
            params_str = ", ".join(block.params)

        output_parts.append(f"{indent}def {block.name}({params_str}):")

        # 函数体：取原始代码文本，重新缩进
        body_lines = source_lines[block.start_line - 1: block.end_line]
        body_text = _reindent_lines(body_lines, inner_indent)
        output_parts.append(body_text)

        # 添加 return 语句
        if block.returns:
            if len(block.returns) == 1:
                output_parts.append(f"{inner_indent}return {block.returns[0]}")
            else:
                output_parts.append(f"{inner_indent}return {', '.join(block.returns)}")

    output_parts.append("")

    # 2) 生成重写后的主函数
    func_start = func_node.lineno - 1
    # 保留函数签名（可能多行）和 docstring
    sig_end = func_node.body[0].lineno - 1  # body 第一条语句之前都是签名
    header_lines = source_lines[func_start:sig_end]

    # 检查 docstring
    body = func_node.body
    has_docstring = (body and isinstance(body[0], ast.Expr)
                     and isinstance(body[0].value, (ast.Constant, ast.Str)))
    if has_docstring:
        doc_end = body[0].end_lineno or body[0].lineno
        header_lines = source_lines[func_start:doc_end]

    output_parts.append("\n".join(header_lines))

    # 生成主函数体：对每个 segment，如果是被提取的块 → 调用子函数，否则保留原代码
    extracted_ranges = {}
    for block in plan.blocks:
        for line_no in range(block.start_line, block.end_line + 1):
            extracted_ranges[line_no] = block

    # 遍历原始函数体的每一行
    body_start = (body[1].lineno if has_docstring and len(body) > 1
                  else body[0].lineno)
    func_end = func_node.end_lineno or func_node.lineno

    current_line = body_start
    seen_blocks = set()

    while current_line <= func_end:
        if current_line in extracted_ranges:
            block = extracted_ranges[current_line]
            if id(block) not in seen_blocks:
                seen_blocks.add(id(block))
                # 生成调用语句
                call = _make_call_stmt(block, inner_indent, is_method)
                output_parts.append(call)
            current_line = block.end_line + 1
        else:
            # 保留原始行
            if current_line - 1 < len(source_lines):
                output_parts.append(source_lines[current_line - 1])
            current_line += 1

    return "\n".join(output_parts)


def _make_call_stmt(block: ExtractedBlock, indent: str, is_method: bool) -> str:
    """生成调用子函数的语句。"""
    if is_method:
        call = f"self.{block.name}({', '.join(block.params)})"
    else:
        call = f"{block.name}({', '.join(block.params)})"

    if block.returns:
        if len(block.returns) == 1:
            return f"{indent}{block.returns[0]} = {call}"
        else:
            return f"{indent}{', '.join(block.returns)} = {call}"
    else:
        return f"{indent}{call}"


def _detect_indent(source_lines: list[str], node: ast.FunctionDef) -> str:
    """检测函数定义的缩进。"""
    line = source_lines[node.lineno - 1]
    return line[: len(line) - len(line.lstrip())]


def _reindent_lines(lines: list[str], target_indent: str) -> str:
    """将代码行重新缩进到目标级别。"""
    if not lines:
        return ""
    # 找到最小缩进
    min_indent = float('inf')
    for line in lines:
        stripped = line.lstrip()
        if stripped:
            min_indent = min(min_indent, len(line) - len(stripped))
    if min_indent == float('inf'):
        min_indent = 0

    result = []
    for line in lines:
        stripped = line.lstrip()
        if stripped:
            old_indent = len(line) - len(stripped)
            extra = old_indent - min_indent
            result.append(target_indent + " " * extra + stripped)
        else:
            result.append("")
    return "\n".join(result)


# ═══════════════════════════════════════════════════════════════════════════
#  第三部分：文件拆分与分类建档
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MovedItem:
    """要从原文件移出的条目。"""
    name: str
    kind: str              # "class" | "function"
    start_line: int
    end_line: int
    source_text: str       # 完整源码文本
    needed_imports: list[str]   # 该条目依赖的 import 语句


@dataclass
class NewModule:
    """要创建的新模块文件。"""
    filename: str
    reason: str
    items: list[MovedItem]
    header_imports: list[str]


@dataclass
class FileSplitPlan:
    """一个文件的拆分计划。"""
    source_path: str
    new_modules: list[NewModule]
    remaining_imports: list[str]   # 原文件中添加的 from xxx import ... 语句


def _analyze_file_for_split(
    file_path: str,
    source: str,
    tree: ast.Module,
    max_file_lines: int = MAX_FILE_LINES,
    max_classes: int = MAX_CLASSES_PER_FILE,
    max_funcs: int = MAX_FUNCS_PER_FILE,
    min_group: int = MIN_GROUP_SIZE,
) -> Optional[FileSplitPlan]:
    """
    分析一个文件，生成拆分计划。
    全面分析 import 依赖和同文件交叉引用。
    """
    lines = source.splitlines()
    total_lines = len(lines)

    # 收集顶层定义
    classes: list[ast.ClassDef] = []
    functions: list[ast.FunctionDef] = []
    import_nodes: list[ast.stmt] = []
    # 顶层变量赋值 (常量等)
    top_assigns: list[ast.stmt] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            import_nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            top_assigns.append(node)

    # 判断是否需要拆分
    needs_split = (
        total_lines > max_file_lines
        or len(classes) > max_classes
        or len(functions) > max_funcs
    )
    if not needs_split:
        return None

    # 收集原文件中所有顶层定义名 (类名 + 函数名 + 顶层变量名)
    all_defined_names: set[str] = set()
    for cls in classes:
        all_defined_names.add(cls.name)
    for func in functions:
        all_defined_names.add(func.name)
    for assign in top_assigns:
        if isinstance(assign, ast.Assign):
            for t in assign.targets:
                if isinstance(t, ast.Name):
                    all_defined_names.add(t.id)
        elif isinstance(assign, ast.AnnAssign) and isinstance(assign.target, ast.Name):
            all_defined_names.add(assign.target.id)

    # 收集所有 import 文本 (用于给新文件完整复制)
    all_import_texts = []
    for node in import_nodes:
        start = node.lineno - 1
        end = (node.end_lineno or node.lineno)
        all_import_texts.append("\n".join(lines[start:end]))

    # 收集顶层常量/变量文本 (需要复制到依赖它们的新文件)
    top_var_texts: dict[str, str] = {}
    for assign in top_assigns:
        start = assign.lineno - 1
        end = assign.end_lineno or assign.lineno
        text = "\n".join(lines[start:end])
        if isinstance(assign, ast.Assign):
            for t in assign.targets:
                if isinstance(t, ast.Name):
                    top_var_texts[t.id] = text
        elif isinstance(assign, ast.AnnAssign) and isinstance(assign.target, ast.Name):
            top_var_texts[assign.target.id] = text

    # 收集所有顶层函数/类的源码文本 (用于复制到新文件，避免循环导入)
    top_item_texts: dict[str, str] = {}
    top_item_imports: dict[str, list[str]] = {}
    for cls in classes:
        start = cls.lineno - 1
        if cls.decorator_list:
            start = cls.decorator_list[0].lineno - 1
        end = cls.end_lineno or cls.lineno
        top_item_texts[cls.name] = "\n".join(lines[start:end])
        same_deps: set[str] = set()
        top_item_imports[cls.name] = _find_needed_imports(
            cls, import_nodes, lines,
            all_defined_names=all_defined_names,
            same_file_names_used=same_deps,
        )
    for func in functions:
        start = func.lineno - 1
        if func.decorator_list:
            start = func.decorator_list[0].lineno - 1
        end = func.end_lineno or func.lineno
        top_item_texts[func.name] = "\n".join(lines[start:end])
        same_deps: set[str] = set()
        top_item_imports[func.name] = _find_needed_imports(
            func, import_nodes, lines,
            all_defined_names=all_defined_names,
            same_file_names_used=same_deps,
        )

    new_modules: list[NewModule] = []
    all_moved_names: set[str] = set()

    # 记录每个条目对同文件其他定义的交叉引用
    cross_refs: dict[str, set[str]] = {}  # item_name → {依赖的同文件名}

    # ── 策略 A：每个类移到独立文件 ──
    if len(classes) > max_classes:
        for cls_node in classes:
            snake_name = _camel_to_snake(cls_node.name)
            filename = f"{snake_name}.py"

            start = cls_node.lineno - 1
            if cls_node.decorator_list:
                start = cls_node.decorator_list[0].lineno - 1
            end = cls_node.end_lineno or cls_node.lineno
            cls_text = "\n".join(lines[start:end])

            same_file_deps: set[str] = set()
            needed = _find_needed_imports(
                cls_node, import_nodes, lines,
                all_defined_names=all_defined_names,
                same_file_names_used=same_file_deps,
            )
            cross_refs[cls_node.name] = same_file_deps

            new_modules.append(NewModule(
                filename=filename,
                reason=f"类 {cls_node.name} 拆分为独立模块",
                items=[MovedItem(
                    name=cls_node.name, kind="class",
                    start_line=start + 1, end_line=end,
                    source_text=cls_text, needed_imports=needed,
                )],
                header_imports=needed,
            ))
            all_moved_names.add(cls_node.name)

    # ── 策略 B：按前缀分组函数 ──
    if len(functions) > max_funcs or total_lines > max_file_lines:
        prefix_groups: dict[str, list[ast.FunctionDef]] = defaultdict(list)

        for func in functions:
            parts = func.name.split("_")
            if len(parts) >= 2 and not func.name.startswith("_"):
                prefix_groups[parts[0]].append(func)

        for prefix, group in prefix_groups.items():
            if len(group) < min_group:
                continue

            filename = f"{prefix}_utils.py"
            items = []
            module_imports = set()
            group_names = {f.name for f in group}
            group_cross_refs: set[str] = set()

            for func_node in group:
                start = func_node.lineno - 1
                if func_node.decorator_list:
                    start = func_node.decorator_list[0].lineno - 1
                end = func_node.end_lineno or func_node.lineno
                func_text = "\n".join(lines[start:end])

                same_file_deps: set[str] = set()
                needed = _find_needed_imports(
                    func_node, import_nodes, lines,
                    all_defined_names=all_defined_names,
                    same_file_names_used=same_file_deps,
                )
                module_imports.update(needed)

                # 同组内的互相引用不算交叉依赖
                external_deps = same_file_deps - group_names
                group_cross_refs |= external_deps
                cross_refs[func_node.name] = same_file_deps

                items.append(MovedItem(
                    name=func_node.name, kind="function",
                    start_line=start + 1, end_line=end,
                    source_text=func_text, needed_imports=needed,
                ))
                all_moved_names.add(func_node.name)

            # 把组级别的交叉依赖也记录
            for name in group_names:
                cross_refs.setdefault(name, set())
                cross_refs[name] |= group_cross_refs

            new_modules.append(NewModule(
                filename=filename,
                reason=f"前缀 '{prefix}_' 的 {len(group)} 个函数归组",
                items=items,
                header_imports=sorted(module_imports),
            ))

    if not new_modules:
        return None

    # ── 解决交叉依赖 ──
    # 建立 "名称 → 它在哪个新模块" 的映射
    name_to_module: dict[str, str] = {}
    for mod in new_modules:
        mod_name = mod.filename.replace(".py", "")
        for item in mod.items:
            name_to_module[item.name] = mod_name

    # 为每个新模块补充交叉引用的 import
    for mod in new_modules:
        mod_name = mod.filename.replace(".py", "")
        extra_imports: set[str] = set()
        extra_var_defs: set[str] = set()

        extra_copy_items: list[str] = []  # 需要复制源码的同文件定义名

        for item in mod.items:
            deps = cross_refs.get(item.name, set())
            for dep_name in deps:
                if dep_name in name_to_module:
                    dep_module = name_to_module[dep_name]
                    if dep_module != mod_name:
                        extra_imports.add(f"from {dep_module} import {dep_name}")
                elif dep_name in top_var_texts:
                    # 依赖顶层常量/变量 → 复制定义到新文件
                    extra_var_defs.add(dep_name)
                elif dep_name in top_item_texts:
                    # 依赖未被移出的同文件函数/类 → 复制源码到新文件（避免循环导入）
                    extra_copy_items.append(dep_name)
                    # 同时需要把被复制项的 import 依赖也带上
                    if dep_name in top_item_imports:
                        for imp in top_item_imports[dep_name]:
                            extra_imports.add(imp)

        # 追加到 header_imports
        if extra_imports:
            existing = set(mod.header_imports)
            for imp in sorted(extra_imports):
                if imp not in existing:
                    mod.header_imports.append(imp)

        # 收集需要复制到新文件头部的代码 (常量 + 未移出的函数/类)
        prefix_parts = []
        if extra_var_defs:
            for name in sorted(extra_var_defs):
                if name in top_var_texts:
                    prefix_parts.append(top_var_texts[name])
        if extra_copy_items:
            seen_copy = set()
            for name in extra_copy_items:
                if name not in seen_copy and name in top_item_texts:
                    seen_copy.add(name)
                    prefix_parts.append(top_item_texts[name])

        if prefix_parts and mod.items:
            prefix_code = "\n\n\n".join(prefix_parts)
            mod.items[0] = MovedItem(
                name=mod.items[0].name,
                kind=mod.items[0].kind,
                start_line=mod.items[0].start_line,
                end_line=mod.items[0].end_line,
                source_text=prefix_code + "\n\n\n" + mod.items[0].source_text,
                needed_imports=mod.items[0].needed_imports,
            )

    # 原文件中需要添加的 import (不带点号，直接 from module import ...)
    remaining_imports = []
    for mod in new_modules:
        module_name = mod.filename.replace(".py", "")
        names = [item.name for item in mod.items]
        remaining_imports.append(f"from {module_name} import {', '.join(names)}")

    return FileSplitPlan(
        source_path=file_path,
        new_modules=new_modules,
        remaining_imports=remaining_imports,
    )


def _collect_all_refs(node: ast.AST) -> set[str]:
    """
    深度收集 AST 节点中引用到的所有名称。
    覆盖：变量读取、函数调用、装饰器、类型注解、基类、异常类型等。
    """
    refs = set()
    for child in ast.walk(node):
        # 直接名称引用 (变量、函数调用、类引用)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            refs.add(child.id)
        # module.attr 形式 → 取最左边的名字
        elif isinstance(child, ast.Attribute):
            n = child
            while isinstance(n, ast.Attribute):
                n = n.value
            if isinstance(n, ast.Name):
                refs.add(n.id)
        # 装饰器 @decorator
        elif isinstance(child, ast.FunctionDef) or isinstance(child, ast.AsyncFunctionDef):
            for dec in child.decorator_list:
                if isinstance(dec, ast.Name):
                    refs.add(dec.id)
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                    refs.add(dec.func.id)
                elif isinstance(dec, ast.Attribute):
                    n = dec
                    while isinstance(n, ast.Attribute):
                        n = n.value
                    if isinstance(n, ast.Name):
                        refs.add(n.id)
            # 函数参数的类型注解
            for arg in child.args.args + child.args.posonlyargs + child.args.kwonlyargs:
                if arg.annotation:
                    refs |= _refs_from_annotation(arg.annotation)
            # 返回值注解
            if child.returns:
                refs |= _refs_from_annotation(child.returns)
        # 类的基类
        elif isinstance(child, ast.ClassDef):
            for base in child.bases:
                if isinstance(base, ast.Name):
                    refs.add(base.id)
                elif isinstance(base, ast.Attribute):
                    n = base
                    while isinstance(n, ast.Attribute):
                        n = n.value
                    if isinstance(n, ast.Name):
                        refs.add(n.id)
            for dec in child.decorator_list:
                if isinstance(dec, ast.Name):
                    refs.add(dec.id)
        # 变量类型注解  x: SomeType = ...
        elif isinstance(child, ast.AnnAssign) and child.annotation:
            refs |= _refs_from_annotation(child.annotation)
        # 字符串内的类型引用 (简单处理)
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # 跳过普通字符串，只处理可能是类型的
            pass

    return refs


def _refs_from_annotation(node: ast.AST) -> set[str]:
    """从类型注解中提取引用的名称。"""
    refs = set()
    if isinstance(node, ast.Name):
        refs.add(node.id)
    elif isinstance(node, ast.Attribute):
        n = node
        while isinstance(n, ast.Attribute):
            n = n.value
        if isinstance(n, ast.Name):
            refs.add(n.id)
    elif isinstance(node, ast.Subscript):
        refs |= _refs_from_annotation(node.value)
        refs |= _refs_from_annotation(node.slice)
    elif isinstance(node, ast.Tuple):
        for elt in node.elts:
            refs |= _refs_from_annotation(elt)
    elif isinstance(node, ast.BinOp):  # X | Y 形式的 union
        refs |= _refs_from_annotation(node.left)
        refs |= _refs_from_annotation(node.right)
    elif isinstance(node, ast.Constant):
        pass  # 字符串字面量注解暂不解析
    for child in ast.iter_child_nodes(node):
        refs |= _refs_from_annotation(child)
    return refs


def _find_needed_imports(
    node: ast.AST,
    import_nodes: list[ast.stmt],
    source_lines: list[str],
    all_defined_names: set[str] | None = None,
    same_file_names_used: set[str] | None = None,
) -> list[str]:
    """
    找出一个 AST 节点用到了哪些 import。

    参数:
        node: 要分析的 AST 节点 (类或函数)
        import_nodes: 原文件中的所有 import 节点
        source_lines: 原文件源码行
        all_defined_names: 原文件中所有顶层定义的名称 (类名、函数名)
        same_file_names_used: [输出] 如果提供，会把引用到的同文件其他定义名填入
    """
    refs = _collect_all_refs(node)

    # 匹配 import 语句
    needed = []
    imported_names = set()  # 被 import 覆盖的名字

    for imp in import_nodes:
        imp_start = imp.lineno - 1
        imp_end = imp.end_lineno or imp.lineno
        imp_text = "\n".join(source_lines[imp_start:imp_end])

        matched = False
        if isinstance(imp, ast.Import):
            for alias in imp.names:
                used_name = alias.asname or alias.name.split(".")[0]
                imported_names.add(used_name)
                if used_name in refs:
                    matched = True
        elif isinstance(imp, ast.ImportFrom):
            for alias in imp.names:
                used_name = alias.asname or alias.name
                imported_names.add(used_name)
                if used_name in refs:
                    matched = True

        if matched:
            needed.append(imp_text)

    # 检测对同文件其他定义的依赖 (不是 import 来的，而是同文件定义的)
    if all_defined_names is not None and same_file_names_used is not None:
        # 排除自身名称
        self_name = None
        if isinstance(node, ast.ClassDef):
            self_name = node.name
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self_name = node.name

        for name in refs:
            if name in all_defined_names and name not in imported_names and name != self_name:
                same_file_names_used.add(name)

    return needed


def _camel_to_snake(name: str) -> str:
    result = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            result.append("_")
        result.append(ch.lower())
    return "".join(result)


# ═══════════════════════════════════════════════════════════════════════════
#  第四部分：执行引擎
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RefactorAction:
    """一个重构操作。"""
    kind: str           # "split_func" | "split_file" | "create_module"
    file_path: str
    description: str
    details: list[str]
    # 执行数据
    func_plan: Optional[FuncSplitPlan] = None
    file_plan: Optional[FileSplitPlan] = None


def analyze_project(root_dir: str) -> list[RefactorAction]:
    """扫描项目，生成所有重构操作计划。"""
    root = Path(root_dir).resolve()
    actions: list[RefactorAction] = []

    py_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))

    for filepath in sorted(py_files):
        try:
            source = Path(filepath).read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=filepath)
        except (SyntaxError, Exception):
            continue

        rel_path = os.path.relpath(filepath, root)
        source_lines = source.splitlines()

        # ── 长函数拆分 ──
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 判断是否是方法
                class_name = None
                for parent_node in ast.walk(tree):
                    if isinstance(parent_node, ast.ClassDef):
                        for child in ast.iter_child_nodes(parent_node):
                            if child is node:
                                class_name = parent_node.name

                plan = _plan_function_split(node, rel_path, class_name, MAX_FUNC_LINES)
                if plan:
                    display = f"{class_name}.{plan.func_name}" if class_name else plan.func_name
                    end_l = node.end_lineno or node.lineno
                    num_l = end_l - node.lineno + 1
                    details = [f"函数 {display} 共 {num_l} 行，将拆分为 {len(plan.blocks)} 个子函数:"]
                    for b in plan.blocks:
                        ret_info = f" → 返回 {', '.join(b.returns)}" if b.returns else ""
                        details.append(
                            f"  {b.name}({', '.join(b.params)})"
                            f"  [第{b.start_line}-{b.end_line}行]{ret_info}"
                        )
                    actions.append(RefactorAction(
                        kind="split_func", file_path=rel_path,
                        description=f"拆分长函数: {display}",
                        details=details, func_plan=plan,
                    ))

        # ── 文件拆分 ──
        file_plan = _analyze_file_for_split(rel_path, source, tree)
        if file_plan:
            details = [f"文件 {rel_path} ({len(source_lines)} 行) 将拆分为:"]
            for mod in file_plan.new_modules:
                names = [item.name for item in mod.items]
                details.append(f"  → {mod.filename}: {', '.join(names)}")
                details.append(f"    原因: {mod.reason}")
            actions.append(RefactorAction(
                kind="split_file", file_path=rel_path,
                description=f"拆分文件: {rel_path}",
                details=details, file_plan=file_plan,
            ))

    return actions


def apply_file_split(action: RefactorAction, root_dir: str, backup: bool = False):
    """执行文件拆分操作。"""
    plan = action.file_plan
    if not plan:
        return

    root = Path(root_dir).resolve()
    source_path = root / plan.source_path
    source = source_path.read_text(encoding="utf-8", errors="replace")
    source_lines = source.splitlines()
    source_dir = source_path.parent

    if backup:
        shutil.copy2(source_path, str(source_path) + ".bak")

    # 确保有 __init__.py
    init_path = source_dir / "__init__.py"
    if not init_path.exists():
        init_path.write_text("", encoding="utf-8")

    # 1) 创建新模块文件
    for mod in plan.new_modules:
        mod_path = source_dir / mod.filename
        parts = []

        # 文件头注释
        parts.append(f'"""从 {Path(plan.source_path).name} 拆分 - {mod.reason}"""')

        # import 语句
        if mod.header_imports:
            parts.append("")
            for imp in mod.header_imports:
                parts.append(imp)

        # 各条目代码
        for item in mod.items:
            parts.append("")
            parts.append("")
            parts.append(item.source_text)

        content = "\n".join(parts) + "\n"
        mod_path.write_text(content, encoding="utf-8")
        print(f"  创建: {mod_path.relative_to(root)}")

    # 2) 重写原文件：删除已移走的代码，添加 import
    moved_ranges = set()
    for mod in plan.new_modules:
        for item in mod.items:
            for ln in range(item.start_line, item.end_line + 1):
                moved_ranges.add(ln)

    # 找到 import 区域的结束位置
    import_insert_line = 0
    try:
        tree = ast.parse(source, filename=str(source_path))
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_insert_line = max(import_insert_line, node.end_lineno or node.lineno)
    except SyntaxError:
        pass

    new_lines = []
    for i, line in enumerate(source_lines):
        line_no = i + 1
        if line_no in moved_ranges:
            # 跳过已移出的行，但保留分隔空行以免连续删除造成格式问题
            if not new_lines or new_lines[-1].strip():
                pass  # 跳过
            continue
        new_lines.append(line)

        # 在 import 区域结束后插入新 import
        if line_no == import_insert_line:
            for imp in plan.remaining_imports:
                new_lines.append(imp)

    # 清理多余空行 (连续超过 2 个的压缩为 2 个)
    cleaned = []
    blank_count = 0
    for line in new_lines:
        if not line.strip():
            blank_count += 1
            if blank_count <= 2:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)

    source_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    print(f"  更新: {plan.source_path}")


def apply_func_split(action: RefactorAction, root_dir: str, backup: bool = False):
    """执行函数拆分操作。"""
    plan = action.func_plan
    if not plan:
        return

    root = Path(root_dir).resolve()
    source_path = root / plan.file_path
    source = source_path.read_text(encoding="utf-8", errors="replace")
    source_lines = source.splitlines()

    if backup:
        bak = str(source_path) + ".bak"
        if not Path(bak).exists():
            shutil.copy2(source_path, bak)

    try:
        tree = ast.parse(source, filename=str(source_path))
    except SyntaxError:
        return

    # 找到目标函数节点
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == plan.func_name and node.lineno == plan.original_lines[0]:
                target = node
                break

    if not target:
        return

    # 生成替换代码
    new_code = _generate_split_code(source_lines, target, plan)

    # 替换原函数区域
    func_start = plan.original_lines[0] - 1
    # 包含装饰器
    if target.decorator_list:
        func_start = target.decorator_list[0].lineno - 1
    func_end = plan.original_lines[1]

    new_lines = source_lines[:func_start] + [new_code] + source_lines[func_end:]
    source_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    print(f"  重构: {plan.file_path} → {plan.func_name}")


# ═══════════════════════════════════════════════════════════════════════════
#  第五部分：预览报告生成
# ═══════════════════════════════════════════════════════════════════════════

def print_plan(actions: list[RefactorAction]):
    """打印重构计划预览。"""
    if not actions:
        print("未发现需要自动重构的内容，代码结构良好！")
        return

    # 分类统计
    func_splits = [a for a in actions if a.kind == "split_func"]
    file_splits = [a for a in actions if a.kind == "split_file"]

    print("=" * 70)
    print("  Python 自动重构计划")
    print("=" * 70)
    print(f"\n  函数拆分: {len(func_splits)} 个")
    print(f"  文件拆分: {len(file_splits)} 个")
    print(f"  总操作数: {len(actions)} 个")

    if func_splits:
        print(f"\n{'─' * 70}")
        print("  函数拆分计划")
        print(f"{'─' * 70}")
        for i, action in enumerate(func_splits, 1):
            print(f"\n  [{i}] {action.description}")
            for line in action.details:
                print(f"      {line}")

    if file_splits:
        print(f"\n{'─' * 70}")
        print("  文件拆分计划")
        print(f"{'─' * 70}")
        for i, action in enumerate(file_splits, 1):
            print(f"\n  [{i}] {action.description}")
            for line in action.details:
                print(f"      {line}")

    print(f"\n{'=' * 70}")
    print("  提示: 添加 --apply 参数执行重构")
    print("        添加 --apply --backup 执行并备份原文件 (.bak)")
    print(f"{'=' * 70}")


def generate_preview_html(actions: list[RefactorAction], output_path: str):
    """生成重构预览 HTML 报告。"""
    func_splits = [a for a in actions if a.kind == "split_func"]
    file_splits = [a for a in actions if a.kind == "split_file"]

    func_cards = []
    for a in func_splits:
        plan = a.func_plan
        blocks_html = ""
        for b in plan.blocks:
            ret = f" → {', '.join(b.returns)}" if b.returns else ""
            blocks_html += (
                f'<div class="block">'
                f'<span class="fn">{b.name}</span>'
                f'(<span class="params">{", ".join(b.params)}</span>)'
                f'<span class="lines">第{b.start_line}-{b.end_line}行</span>'
                f'<span class="ret">{ret}</span>'
                f'</div>'
            )
        func_cards.append(
            f'<div class="card">'
            f'<div class="card-title">{a.description}</div>'
            f'<div class="card-file">{a.file_path}</div>'
            f'<div class="blocks">{blocks_html}</div>'
            f'</div>'
        )

    file_cards = []
    for a in file_splits:
        plan = a.file_plan
        mods_html = ""
        for mod in plan.new_modules:
            names = ", ".join(item.name for item in mod.items)
            mods_html += (
                f'<div class="mod">'
                f'<span class="mod-arrow">→</span>'
                f'<span class="mod-file">{mod.filename}</span>'
                f'<span class="mod-items">{names}</span>'
                f'<div class="mod-reason">{mod.reason}</div>'
                f'</div>'
            )
        file_cards.append(
            f'<div class="card">'
            f'<div class="card-title">{a.description}</div>'
            f'<div class="mods">{mods_html}</div>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>自动重构预览</title>
<style>
:root{{--bg:#0f172a;--s:#1e293b;--bd:#475569;--t:#e2e8f0;--tm:#94a3b8;--a:#38bdf8;--a2:#818cf8;--d:#f87171;--g:#34d399;--w:#fbbf24}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--t);line-height:1.6;padding:40px 20px}}
.ct{{max-width:900px;margin:0 auto}}
h1{{text-align:center;font-size:2em;background:linear-gradient(135deg,var(--a),var(--a2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}}
.sub{{text-align:center;color:var(--tm);margin-bottom:30px}}
.stats{{display:flex;gap:16px;justify-content:center;margin-bottom:30px}}
.stat{{background:var(--s);border:1px solid var(--bd);border-radius:10px;padding:16px 24px;text-align:center}}
.stat b{{font-size:1.8em;display:block}}
.stat span{{color:var(--tm);font-size:.85em}}
.section{{margin-bottom:24px}}
.section h2{{font-size:1.3em;color:var(--a);margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid var(--bd)}}
.card{{background:var(--s);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-bottom:12px}}
.card-title{{font-weight:600;font-size:1.05em;margin-bottom:6px}}
.card-file{{font-family:monospace;color:var(--a);font-size:.9em;margin-bottom:10px}}
.block{{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;margin:6px 0;font-size:.9em;display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.fn{{color:var(--a2);font-weight:600;font-family:monospace}}
.params{{color:var(--tm);font-family:monospace}}
.lines{{color:var(--w);font-size:.8em}}
.ret{{color:var(--g);font-size:.85em}}
.mod{{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:10px 12px;margin:6px 0}}
.mod-arrow{{color:var(--a);font-size:1.2em;margin-right:6px}}
.mod-file{{color:var(--g);font-weight:600;font-family:monospace}}
.mod-items{{color:var(--tm);font-size:.9em;margin-left:12px}}
.mod-reason{{color:var(--a2);font-size:.85em;margin-top:4px}}
.empty{{text-align:center;color:var(--g);padding:40px;font-size:1.2em}}
</style></head><body>
<div class="ct">
<h1>自动重构预览</h1>
<p class="sub">以下操作将在 --apply 后执行</p>
<div class="stats">
<div class="stat"><b style="color:var(--a2)">{len(func_splits)}</b><span>函数拆分</span></div>
<div class="stat"><b style="color:var(--g)">{len(file_splits)}</b><span>文件拆分</span></div>
<div class="stat"><b style="color:var(--a)">{len(actions)}</b><span>总操作数</span></div>
</div>
{"" if actions else '<div class="empty">无需重构，代码结构良好！</div>'}
{"<div class='section'><h2>函数拆分</h2>" + "".join(func_cards) + "</div>" if func_cards else ""}
{"<div class='section'><h2>文件拆分</h2>" + "".join(file_cards) + "</div>" if file_cards else ""}
</div></body></html>"""

    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


# ═══════════════════════════════════════════════════════════════════════════
#  第六部分：快照与测试验证
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FileSnapshot:
    """文件快照，用于回滚。"""
    path: str
    content: Optional[str]   # None 表示文件原本不存在
    existed: bool


def _take_snapshot(file_paths: list[str]) -> list[FileSnapshot]:
    """保存一组文件的当前内容快照。"""
    snapshots = []
    for fp in file_paths:
        p = Path(fp)
        if p.exists():
            snapshots.append(FileSnapshot(
                path=fp,
                content=p.read_text(encoding="utf-8", errors="replace"),
                existed=True,
            ))
        else:
            snapshots.append(FileSnapshot(path=fp, content=None, existed=False))
    return snapshots


def _restore_snapshot(snapshots: list[FileSnapshot]):
    """从快照恢复文件状态。"""
    for snap in snapshots:
        p = Path(snap.path)
        if snap.existed:
            p.write_text(snap.content, encoding="utf-8")
        else:
            # 文件原本不存在 → 删除新建的文件
            if p.exists():
                p.unlink()


def _get_affected_paths(action: RefactorAction, root_dir: str) -> list[str]:
    """获取一个重构操作会影响的所有文件路径。"""
    root = Path(root_dir).resolve()
    paths = []

    if action.kind == "split_file" and action.file_plan:
        plan = action.file_plan
        source_path = root / plan.source_path
        paths.append(str(source_path))
        source_dir = source_path.parent
        for mod in plan.new_modules:
            paths.append(str(source_dir / mod.filename))
        # __init__.py 可能被创建
        init_path = source_dir / "__init__.py"
        paths.append(str(init_path))

    elif action.kind == "split_func" and action.func_plan:
        plan = action.func_plan
        paths.append(str(root / plan.file_path))

    return paths


def run_test_command(test_cmd: str, cwd: str) -> tuple[bool, str]:
    """
    执行测试命令。
    返回 (是否通过, 输出摘要)。
    """
    print(f"  运行测试: {test_cmd}")
    try:
        result = subprocess.run(
            test_cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        passed = result.returncode == 0

        # 提取摘要 (最后几行通常包含结果)
        output = result.stdout + result.stderr
        summary_lines = [l for l in output.strip().splitlines() if l.strip()]
        summary = "\n".join(summary_lines[-5:]) if summary_lines else "(无输出)"

        if passed:
            print(f"  ✓ 测试通过")
        else:
            print(f"  ✗ 测试失败!")
            print(f"    {summary}")

        return passed, summary

    except subprocess.TimeoutExpired:
        print(f"  ✗ 测试超时 (300s)")
        return False, "测试执行超时"
    except Exception as e:
        print(f"  ✗ 测试执行错误: {e}")
        return False, str(e)


# ═══════════════════════════════════════════════════════════════════════════
#  CLI 入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global MAX_FUNC_LINES, MAX_FILE_LINES
    parser = argparse.ArgumentParser(
        description="Python 自动重构工具 - 拆分长函数、长文件，按分类创建新模块",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              python refactor_auto.py .                          # 预览重构计划
              python refactor_auto.py . --apply                  # 执行重构
              python refactor_auto.py . --apply --backup         # 执行并备份
              python refactor_auto.py . --preview-html plan.html # 生成预览报告
              python refactor_auto.py . --max-func-lines 50      # 自定义阈值
              python refactor_auto.py . --apply --test "pytest tests/"  # 重构后跑测试

            操作模式:
              默认 (dry-run):  只分析和展示计划，不修改任何文件
              --apply:         执行重构，修改/创建文件
              --apply --backup: 执行重构，原文件备份为 .bak

            测试验证:
              --test "pytest xxx":  每步重构后运行测试，失败则自动回滚该步操作
              确保重构不破坏现有功能

            安全说明:
              建议先在 dry-run 模式查看计划，确认后再 --apply
              建议在版本控制下使用，方便回退
        """))
    parser.add_argument("project_dir", help="要重构的 Python 项目目录")
    parser.add_argument("--apply", action="store_true", help="执行重构 (默认仅预览)")
    parser.add_argument("--backup", action="store_true", help="执行前备份原文件为 .bak")
    parser.add_argument("--preview-html", metavar="PATH", help="生成 HTML 预览报告")
    parser.add_argument("--max-func-lines", type=int, default=MAX_FUNC_LINES,
                        help=f"函数行数阈值 (默认: {MAX_FUNC_LINES})")
    parser.add_argument("--max-file-lines", type=int, default=MAX_FILE_LINES,
                        help=f"文件行数阈值 (默认: {MAX_FILE_LINES})")
    parser.add_argument("--file-only", action="store_true", help="只执行文件拆分")
    parser.add_argument("--func-only", action="store_true", help="只执行函数拆分")
    parser.add_argument("--test", metavar="CMD",
                        help="每步重构后运行的测试命令 (如 'pytest tests/'), 失败则回滚")

    args = parser.parse_args()

    MAX_FUNC_LINES = args.max_func_lines
    MAX_FILE_LINES = args.max_file_lines

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.isdir(project_dir):
        print(f"错误: 目录不存在 - {project_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"正在分析: {project_dir}\n")
    actions = analyze_project(project_dir)

    # 过滤操作类型
    if args.file_only:
        actions = [a for a in actions if a.kind == "split_file"]
    elif args.func_only:
        actions = [a for a in actions if a.kind == "split_func"]

    if args.preview_html:
        html_path = generate_preview_html(actions, args.preview_html)
        print(f"预览报告已生成: {os.path.abspath(html_path)}")

    if not args.apply:
        print_plan(actions)
        return

    # ── 执行重构 ──
    if not actions:
        print("未发现需要重构的内容。")
        return

    test_cmd = args.test

    # 如果指定了测试命令，先验证测试在重构前能通过
    if test_cmd:
        print("先验证现有测试是否通过...\n")
        passed, _ = run_test_command(test_cmd, project_dir)
        if not passed:
            print("\n错误: 重构前测试就已失败，请先修复测试再进行重构。")
            sys.exit(1)
        print("  现有测试通过，开始重构。\n")

    print(f"开始执行重构 ({len(actions)} 个操作)...\n")

    # 先执行文件拆分（因为函数拆分依赖文件存在）
    file_actions = [a for a in actions if a.kind == "split_file"]
    func_actions = [a for a in actions if a.kind == "split_func"]

    applied_count = 0
    rolled_back_count = 0

    for action in file_actions:
        print(f"\n[文件拆分] {action.description}")
        affected = _get_affected_paths(action, project_dir)
        snapshots = _take_snapshot(affected)

        apply_file_split(action, project_dir, backup=args.backup)

        if test_cmd:
            passed, summary = run_test_command(test_cmd, project_dir)
            if not passed:
                print(f"  ↩ 回滚: {action.description}")
                _restore_snapshot(snapshots)
                rolled_back_count += 1
                continue

        applied_count += 1

    for action in func_actions:
        print(f"\n[函数拆分] {action.description}")
        affected = _get_affected_paths(action, project_dir)
        snapshots = _take_snapshot(affected)

        apply_func_split(action, project_dir, backup=args.backup)

        if test_cmd:
            passed, summary = run_test_command(test_cmd, project_dir)
            if not passed:
                print(f"  ↩ 回滚: {action.description}")
                _restore_snapshot(snapshots)
                rolled_back_count += 1
                continue

        applied_count += 1

    print(f"\n{'=' * 60}")
    print(f"  重构完成！")
    print(f"  成功应用: {applied_count} 个操作")
    if rolled_back_count:
        print(f"  测试失败回滚: {rolled_back_count} 个操作")
    if args.backup:
        print("  原文件已备份为 .bak 文件。")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
