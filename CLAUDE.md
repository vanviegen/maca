# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MACA (Minimal AI Coding Assistant) is a Python-based coding assistant that uses git worktrees for isolation, OpenRouter API for LLM access, and Docker/Podman for safe command execution.

## Essential Commands

### Development
```bash
./maca                    # Run interactively
./maca "your task here"   # Run with task argument
./maca --help             # Show CLI options
```

### Environment Setup
MACA auto-creates a virtual environment at `~/.cache/maca-venv-1` with required dependencies (prompt-toolkit).

Set the OpenRouter API key:
```bash
export OPENROUTER_API_KEY="your-key-here"
```

## High-Level Architecture

### Single-Agent System
MACA provides a single AI agent that accomplishes coding tasks through tool calls.

### Key Components

**Git Worktree Isolation** (`git_ops.py`)
- Each session gets isolated worktree at `.maca/<session_id>/tree/`
- Session branch: `maca-<session_id>`
- On merge: squash commits, preserve original chain in `maca/<descriptive-name>` branch
- `.scratch/` directory for temporary files (git-ignored, never committed)

**Tool System** (`tools.py`)
- Reflection-based schema generation from Python functions
- Single `_TOOLS` registry for all tools
- `@tool` decorator registers tools (no arguments needed)
- All tools are always available to the context
- Tools return tuples: (immediate_result, context_summary)
- All file paths are relative to worktree

**Context Management** (`maca.py`)
- Single `MACA` class for the coding assistant
- Loads system prompt from `prompt.md` (plain markdown, no headers)
- Model specified via -m command line argument (default: anthropic/claude-sonnet-4.5)
- Tracks and reports git HEAD changes between invocations
- OpenRouter API used for all LLM calls (via `call_llm` in utils.py)
- Automatic context size management:
  - When tool output >500 chars: shown once with ephemeral cache, then replaced with summary
  - Uses Anthropic ephemeral cache control markers
  - `process_tool_call_from_message()` method handles tool execution and result processing

**State Tracking** (AGENTS.md and code_map)
- AGENTS.md and project code_map loaded at initialization as system messages
- After each tool call that creates a git commit, both are regenerated
- If either changed, a diff is added as a system message
- When 8 state update messages accumulate, history is rewritten: all diffs removed and replaced with new baselines
- This balances token caching (changes are visible) with context efficiency (don't keep growing forever)

**Session Logging** (`logger.py`)
- Human-readable logs in `.maca/<session_id>/main.log`
- HEREDOC format for multiline values
- Tracks: LLM calls, tool invocations, tokens, costs, git changes

**Docker Execution** (`docker_ops.py`)
- Shell commands run in Docker/Podman containers
- Default: `debian:stable` with build-essential, git, python3
- Worktree mounted into container for file access
- Auto-detects docker/podman at runtime

**Code Map** (`code_map.py`)
- Uses tree-sitter to parse source files for code structure
- Lists ALL files in worktree (respecting .gitignore)
- Shows `path/name.ext [XX lines]` for text files
- Shows `path/name.ext [XX bytes]` for binary files (detected via null byte heuristic)
- For code files: extracts classes, functions, methods with line ranges and cross-references

### Critical Patterns

**.scratch/ Directory**
- For temporary files that shouldn't be committed
- Analysis reports, test outputs, detailed findings
- Never committed to git

**Gitignore Support**
- `get_matching_files()` and `search()` tools support `exclude_files` parameter
- Defaults to `['.gitignore']` to respect gitignore patterns
- Uses `GitignoreMatcher` in utils.py for gitignore semantics (negation, directory patterns, etc.)

**Tool Call Efficiency**
- Batch operations: use `process_files` with batches parameter
- Use glob patterns with exclude_files for flexible file filtering
- Minimize tool calls for efficiency

**process_files**
- Primary tool for reading and processing files
- Takes `batches` parameter: `List[List[Dict[str, Any]]]`
- Each dict specifies: `{path: str, start_line?: int, end_line?: int}`
- Two modes based on batch count:
  - **Single batch**: Load all files, return contents to main loop for coordinated changes
  - **Multiple batches**: Process each batch with separate LLM call, each has access to all tools
- Useful for both coordinated changes (single batch) and mechanical per-file changes (multiple batches)

**update_files**
- Supports optional `summary` parameter for custom context summaries
- When provided, summary is stored in long-term context instead of full tool output

**LLM Call Logic** (`utils.py`)
- `call_llm()` function handles all LLM API interactions
- Used by both `maca.py` main loop and `process_files` tool
- Handles: API call, error handling, logging, usage tracking
- Returns dict with message, cost, and usage

### Important Files

**Core Python Modules**
- `maca.py` - Main orchestration loop and MACA class
- `tools.py` - Tool system with reflection-based schemas
- `git_ops.py` - Git worktree and branch management
- `logger.py` - Human-readable session logs
- `docker_ops.py` - Container execution for shell commands
- `utils.py` - Utilities: cprint, gitignore parsing, LLM call logic, Color dataclass
- `code_map.py` - Code structure extraction and file listing

**System Prompt**
- `prompt.md` - Plain markdown system prompt (no headers)

**Entry Points**
- `maca` - Shell wrapper that creates venv and runs `maca.py`
- `maca.py` - Python entry point with argparse

## Working with MACA

### Understanding the Flow
1. User provides task
2. Assistant executes tools to accomplish the task
3. Each tool call creates a git commit
4. After commits, AGENTS.md and code_map diffs are tracked (if changed)
5. When complete, assistant calls `complete()` → user approves → squash merge to main

### Key Design Principles
- **Isolation**: Each session in separate worktree/branch
- **Efficiency**: Batch operations, minimize tool calls
- **Traceability**: Git commits per tool, human-readable logs, state diffs
- **Safety**: Docker for command execution
- **Autonomy**: Works independently with minimal back-and-forth
- **Context Management**: State tracking with periodic history rewrites for efficiency

### Testing and Debugging
- Session logs in `.maca/<session_id>/main.log`
- Git history shows each tool's changes
- `.scratch/` for temporary analysis/debugging files

## Code Modification Guidelines

### Adding New Tools
1. Define function in `tools.py` with proper type hints
2. Add comprehensive docstring (generates schema description)
3. Decorate with `@tool` (no arguments)
4. Return tuple: (immediate_result, context_summary)
5. Schema auto-generated via reflection
6. Tool is automatically available to all contexts

### Modifying the Prompt
- `prompt.md` is plain markdown (no headers)
- Changes to `prompt.md` affect new sessions
- Keep prompts focused and actionable

### Git Operations
- Always use functions from `git_ops.py`
- Never manually manipulate `.maca/` directory
- Worktree paths returned by `create_session_worktree()`
- All commits exclude `.scratch/` and `.maca/` directories
