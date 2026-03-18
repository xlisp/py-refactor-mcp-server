# PyRefactor — Code Review Tools Built for Humans

Static analysis, auto-refactoring, structural diff, and MCP server for Python projects.

> **Vision:** Everything in this project is designed for **humans to read, review, and understand code** — not for machines. Machines can parse anything; humans cannot. The bottleneck in software is always human comprehension. We build tools that make code **easy to see, easy to input, and easy to learn**.

## Why This Exists

Code review is fundamentally a human activity. Yet most tools output walls of text that only machines love. We believe:

- **Easy to see** — Visual, structural diffs that show *what changed and why*, not just which lines moved. Color-coded, interactive, side-by-side HTML reports you can click through.
- **Easy to input** — Lisp taught us that the simplest syntax is the most powerful. One command, one commit hash — that's all you need. No config files, no setup ceremony.
- **Easy to learn** — Every report is self-explanatory. Open it in a browser, click around, understand. No manual required.

## Vision & Roadmap

```
Built for humans who review code — not machines that process it.
```

### Core Principles

1. **Human-first output** — Every report, every diff, every suggestion is designed for a person sitting in front of a screen. If it's not immediately clear at a glance, it's a bug.

2. **Visual thinking** — Code is a tree, not a list of lines. Our structural diff sees code the way programmers think about it: functions, classes, blocks — not line 47 vs line 52.

3. **Minimal input, maximum insight** — Inspired by Lisp's elegance: the simplest possible interface that expresses the full intent. One command gets you a complete code review. One commit hash gets you a full structural diff.

4. **Lower the barrier to learning** — A junior developer should be able to open our HTML report and immediately understand what changed and what needs attention. The tool teaches through its output.

### Roadmap

- [ ] **Cross-file move detection** — Track functions that moved between files across commits
- [ ] **Semantic rename detection** — Identify renamed variables/functions even when the name is completely different
- [ ] **Review comments overlay** — Annotate structural diffs with AI-generated review suggestions
- [ ] **Multi-commit timeline** — Visualize how a function/class evolved across a series of commits
- [ ] **Live MCP review** — AI editor reads the structural diff in real-time and suggests improvements during code review
- [ ] **Support more languages** — Extend structural diff beyond Python (JavaScript, Go, Rust)
- [ ] **Lisp-style input DSL** — A tiny expression language for composing complex code queries: `(diff (commit HEAD~3) (files "src/**/*.py") (ignore tests))`

---

## Quick Start

```bash
git clone <repo-url>
cd py-refactor-mcp-server

# Code review report (no dependencies needed)
python refactor_analyzer.py /path/to/project

# Structural diff of a git commit
python ydiff_python.py --commit /path/to/project abc1234

# Compare two files
python ydiff_python.py old.py new.py

# Auto-refactor (preview mode)
python refactor_auto.py /path/to/project

# MCP Server (requires: pip install "mcp[cli]")
python refactor_mcp_server.py
```

---

## Tool 1: Code Review Analyzer

Static analysis that scans Python projects for code smells and generates visual HTML reports.

### What It Detects

| Issue | Description | Default Threshold |
|-------|-------------|-------------------|
| Long functions | Function body exceeds line limit | 30 lines |
| Too many params | Excessive function parameters | 5 |
| Too many local vars | Too many variables in one function | 8 |
| High complexity | McCabe cyclomatic complexity | 10 |
| Long files | Single file too large | 400 lines |
| Too many classes | Too many classes in one file | 4 |
| Too many functions | Too many top-level functions in one file | 10 |

### Usage

```bash
# Basic scan
python refactor_analyzer.py /path/to/project

# Custom output path
python refactor_analyzer.py /path/to/project -o report.html

# Custom thresholds
python refactor_analyzer.py . --max-func-lines 50 --max-complexity 15
```

### As a Library

```python
from refactor_analyzer import scan_project, generate_html_report

result = scan_project("/path/to/project")
print(f"Found {len(result.issues)} issues")
for issue in result.issues:
    print(f"  [{issue.severity}] {issue.file}:{issue.line} - {issue.title}")

generate_html_report(result, "report.html")
```

### HTML Report Features

- Health score (0–100) with grade (A/B/C/D)
- Statistics dashboard
- Issue distribution pie chart
- Function length / complexity TOP 15 bar charts
- Filterable issue table (by severity, type, keyword)
- File reorganization suggestion cards
- Dark theme, responsive layout

---

## Tool 2: Auto Refactoring

Automatically splits long functions and large files with dependency tracking and test verification.

### Usage

```bash
# Preview mode (no files modified)
python refactor_auto.py /path/to/project

# Execute refactoring with backup
python refactor_auto.py /path/to/project --apply --backup

# With test verification (auto-rollback on failure)
python refactor_auto.py /path/to/project --apply --test "pytest tests/"

# Only split files / only split functions
python refactor_auto.py . --apply --file-only
python refactor_auto.py . --apply --func-only
```

### How It Works

**Function splitting:**
- Analyzes function body structure via AST (if/elif branches, loops, try/except blocks)
- Automatically infers parameters (variables read from outer scope) and return values (variables written and used later)
- Generates named sub-functions with proper signatures

**File splitting:**
- Classes → individual modules (`UserManager` → `user_manager.py`)
- Functions → grouped by naming prefix (`data_load_*` → `data_utils.py`)
- Tracks imports and generates correct re-exports in the original file

**Test-driven safety:**
1. Runs tests before refactoring to establish baseline
2. Runs tests after each operation
3. Auto-rolls back failed operations
4. Continues with remaining operations

---

## Tool 3: Structural Code Diff (ydiff)

Language-aware structural diff for Python. Inspired by [ydiff](https://github.com/yinwang0/ydiff) (Yin Wang).

Unlike line-based diff, this compares code at the **AST level** — it understands functions, classes, expressions, and can detect **moved code blocks**.

### Compare Two Files

```bash
python ydiff_python.py old.py new.py
# → generates old-new.html
```

### Git Commit Diff Report

```bash
# Diff a specific commit
python ydiff_python.py --commit /path/to/repo abc1234

# Custom output path
python ydiff_python.py --commit . HEAD~1 review.html
```

### What the Report Shows

| Color | Meaning |
|-------|---------|
| **Red background** (left panel) | Old version — deleted code highlighted in red |
| **Green background** (right panel) | New version — inserted code highlighted in green |
| **Gray/blue border** | Matched or moved code — click to jump to counterpart |

### Commit Report Features

- Dark-themed tabbed UI
- File navigator sidebar with status badges (M/A/D/R)
- Per-file structural diff with left-red / right-green panels
- Click any matched element to auto-scroll the other panel
- Handles initial commits, added/deleted files, renames

### Algorithm

1. **Parse** — Python `ast` module → custom Node tree with source positions
2. **Structural diff** — Recursive tree comparison with DP + memoization; same-name definitions are force-matched
3. **Move detection** — Iteratively matches large deletions against large insertions to find relocated code
4. **HTML generation** — Inserts change tags into original source text; embeds interactive navigation JS

---

## Tool 4: MCP Server

Exposes all capabilities to AI editors (Claude Desktop, VSCode, Cursor) via the [Model Context Protocol](https://modelcontextprotocol.io).

### Setup

```bash
pip install "mcp[cli]"
python refactor_mcp_server.py
```

### Configuration

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):
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

**VSCode** (`.vscode/mcp.json`):
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

### Available Tools (11)

| Tool | Description |
|------|-------------|
| `scan_project` | Full project scan — issues, suggestions, health score |
| `analyze_file` | Single file analysis with function metrics |
| `analyze_function` | Deep analysis of a specific function |
| `find_long_functions` | TOP N longest functions |
| `find_complex_functions` | TOP N highest complexity functions |
| `suggest_file_reorg` | File reorganization suggestions |
| `generate_report` | Generate HTML quality report |
| `health_score` | Quick 0–100 score |
| `auto_refactor` | Auto-refactor (preview or apply) |
| **`ydiff_files`** | Structural diff of two Python files → HTML |
| **`ydiff_commit`** | Structural diff of a git commit → multi-file HTML report |

### Example Conversation

```
User: Generate a structural diff for commit abc1234 in /home/user/myproject

AI:  (calls ydiff_commit) Commit diff report generated: commit-abc1234.html
     3 files changed, 2 Python files with structural diff.
     Open in browser to view side-by-side comparison.

User: How's the code quality of this project?

AI:  (calls health_score) Health score: 72/100 (Grade B)
     17 issues found. Main concern: process_user_data is 89 lines...

User: Show me what changed in utils.py between these two versions

AI:  (calls ydiff_files) Structural diff generated: utils_old-utils_new.html
     Red = deleted, Green = inserted, Gray = moved code.
```

---

## Project Structure

```
py-refactor-mcp-server/
├── refactor_analyzer.py      # Code review analyzer + HTML report
├── refactor_auto.py          # Auto-refactoring engine
├── refactor_mcp_server.py    # MCP Server (11 tools)
├── ydiff_python.py           # Structural code diff (AST-level)
├── sample_project/           # Sample project with code smells
│   └── app.py
├── demo/                     # ydiff demo files
│   ├── v1.py
│   └── v2.py
└── README.md
```

## Requirements

- Python 3.10+
- CLI tools: no third-party dependencies (stdlib only)
- MCP Server: `pip install "mcp[cli]"`
- HTML reports: Chart.js loaded via CDN (needs internet for charts)

## License

MIT
