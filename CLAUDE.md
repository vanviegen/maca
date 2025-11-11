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

**MACA Class** (`maca.py`)
- Single `MACA` class orchestrates all functionality
- Loads system prompt from `prompt.md`
- Prompt file has metadata headers (default_model, tools) separated by blank line
- Default model is `openai/gpt-5-mini` unless overridden by prompt file or at instantiation
- Tracks and reports git HEAD changes between invocations
- OpenRouter API used for all LLM calls
- Manages conversation state and tool execution loop

**Tool System** (`tools.py`)
- Reflection-based schema generation from Python functions
- Single `_TOOLS` registry for all tools
- `@tool` decorator registers tools (no arguments needed)
- Tools listed in prompt.md header determine which tools are available
- All file paths are relative to worktree
- Tools receive context (worktree path, repo root, history) via global variables set by execute_tool

**Session Logging** (`logger.py`)
- Human-readable logs in `.maca/<session_id>/main.log`
- HEREDOC format for multiline values
- Tracks: LLM calls, tool invocations, tokens, costs, git changes

**Docker Execution** (`docker_ops.py`)
- Shell commands run in Docker/Podman containers
- Default: `debian:stable` with build-essential, git, python3
- Worktree mounted into container for file access
- Auto-detects docker/podman at runtime

### Critical Patterns

**.scratch/ Directory**
- For temporary files that shouldn't be committed
- Analysis reports, test outputs, detailed findings
- Never committed to git

**Tool Call Efficiency**
- Batch operations: read multiple files in ONE call
- Use glob patterns with arrays to match multiple file types: `["**/*.py", "**/*.md"]`
- Tools support include/exclude parameters for flexible file filtering
- Minimize tool calls for efficiency

**run_oneshot_per_file**
- Apply a task to multiple files individually using LLM calls
- Each file gets a separate LLM call with custom system prompt
- Useful for mechanical changes across multiple files
- Each LLM call has access to one tool (typically `update_files`)

### Important Files

**Core Python Modules**
- `maca.py` - Main MACA class with orchestration loop and LLM interaction
- `tools.py` - Tool system with reflection-based schemas
- `git_ops.py` - Git worktree and branch management
- `logger.py` - Human-readable session logs
- `docker_ops.py` - Container execution for shell commands

**System Prompt**
- `prompt.md` - System prompt with metadata headers

The prompt file starts with metadata headers:
```markdown
default_model: anthropic/claude-sonnet-4.5
tools: get_user_input, complete, read_files, list_files, update_files, search, shell, run_oneshot_per_file

System prompt content here...
```

**Entry Points**
- `maca` - Shell wrapper that creates venv and runs `run.py`
- `run.py` - Python entry point with argparse that instantiates MACA class

## Working with MACA

### Understanding the Flow
1. User provides task
2. Assistant executes tools to accomplish the task
3. Each tool call creates a git commit
4. When complete, assistant calls `complete()` → user approves → squash merge to main

### Key Design Principles
- **Isolation**: Each session in separate worktree/branch
- **Efficiency**: Batch operations, minimize tool calls
- **Traceability**: Git commits per tool, human-readable logs
- **Safety**: Docker for command execution
- **Autonomy**: Works independently with minimal back-and-forth

### Testing and Debugging
- Session logs in `.maca/<session_id>/main.log`
- Git history shows each tool's changes
- `.scratch/` for temporary analysis/debugging files

## Code Modification Guidelines

### Adding New Tools
1. Define function in `tools.py` with proper type hints
2. Add comprehensive docstring (generates schema description)
3. Decorate with `@tool` (no arguments)
4. Schema auto-generated via reflection
5. Add tool name to `tools:` header in `prompt.md`

### Modifying the Prompt
- `prompt.md` must start with metadata headers (`default_model:`, `tools:`) followed by blank line
- Changes to `prompt.md` affect new sessions
- Keep prompts focused and actionable
- When changing tools available, update the `tools:` header

### Git Operations
- Always use functions from `git_ops.py`
- Never manually manipulate `.maca/` directory
- Worktree paths returned by `create_session_worktree()`
- All commits exclude `.scratch/` and `.maca/` directories
