# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MACA (Multi-Agent Coding Assistant) is a Nim-based multi-agent system that orchestrates specialized AI contexts to accomplish coding tasks. It uses git worktrees for isolation, OpenRouter API for LLM access, and Docker/Podman for safe command execution.

**Note**: This is the Nim version of MACA. The original Python implementation is preserved for reference.

## Essential Commands

### Development
```bash
nimble build              # Build the maca binary
./maca                    # Run interactively
./maca "your task here"   # Run with task argument
./maca --help             # Show CLI options
```

### Environment Setup
Requires Nim >= 2.0.0. Install dependencies with:
```bash
nimble install
```

Set the OpenRouter API key:
```bash
export OPENROUTER_API_KEY="your-key-here"
```

## High-Level Architecture

### Multi-Agent System
MACA orchestrates specialized contexts that communicate through tool calls:

**Main Context** (`context.Context` / `prompts/_main.md`)
- Orchestrates the entire workflow
- Delegates to specialized subcontexts
- Can work directly for simple tasks
- Manages the `.scratch/PLAN.md` file for complex tasks
- Has access to ALL tools (both main-specific and subcontext tools)

**Specialized Subcontexts** (defined by prompt files in `prompts/`)
- `code_analysis`: Analyzes codebases, creates/maintains AGENTS.md
- `research`: Gathers information, web search (use `perplexity/sonar-pro` model)
- `implementation`: Writes and modifies code
- `review`: Reviews code for quality and correctness
- `merge`: Resolves git merge conflicts

**Special Contexts** (prefixed with `_`, cannot be created via `create_subcontext`)
- `_main`: Main orchestrator context
- `_file_processor`: One-shot file processing (used by `run_oneshot_per_file`)

### Key Components

**Git Worktree Isolation** (`git_ops.nim`)
- Each session gets isolated worktree at `.maca/<session_id>/worktree/`
- Session branch: `maca-<session_id>`
- On merge: squash commits, preserve original chain in `maca/<descriptive-name>` branch
- `.scratch/` directory for temporary files (git-ignored, never committed)

**Tool System** (`tools.nim`)
- Explicit schema definition for each tool
- Single `toolRegistry` for all tools
- Tools listed in prompt file headers determine which tools a context can use
- Subcontext tools automatically get `rationale` parameter added to schema
- All file paths are relative to worktree

**Context Management** (`context.nim`)
- Single `Context` type for all context types
- Each context loads system prompts from `prompts/*.md`
- Prompt files have metadata headers (default_model, tools) separated by blank line
- Context type determined by which prompt file is loaded
- Default model is `openai/gpt-5-mini` unless overridden by prompt file or at instantiation
- Contexts track and report git HEAD changes between invocations
- `AGENTS.md` loaded as system message, updates appended as diffs
- OpenRouter API used for all LLM calls

**Session Logging** (`logger.nim`)
- Human-readable logs per context in `.maca/<session_id>/<context_id>.log`
- HEREDOC format for multiline values
- Tracks: LLM calls, tool invocations, tokens, costs, git changes

**Docker Execution** (`docker_ops.nim`)
- Shell commands run in Docker/Podman containers
- Default: `debian:stable`
- Worktree mounted into container for file access
- Auto-detects docker/podman at runtime

### Important Files

**Core Nim Modules** (in `src/maca/`)
- `../maca.nim` - Main entry point that compiles to binary
- `context.nim` - Context type and LLM interaction
- `tools.nim` - Tool system with schema definitions
- `git_ops.nim` - Git worktree and branch management
- `logger.nim` - Human-readable session logs
- `docker_ops.nim` - Container execution for shell commands
- `utils.nim` - Color printing and utility functions

**System Prompts** (in `prompts/`)
- Same as Python version - no changes needed

**Build Configuration**
- `maca.nimble` - Nimble package file with dependencies

## Building and Compilation

```bash
nimble build                    # Build release binary
nimble build --opt:speed        # Optimized build
nim c -r src/maca.nim          # Compile and run directly
nim c -d:release src/maca.nim  # Release mode compile
```

## Dependencies

MACA uses the following Nim packages:
- `nimline` (>=0.1.0) - Terminal readline interface
- `cligen` (>=1.7.0) - Command-line argument parsing
- `jsony` (>=1.1.5) - Fast JSON parsing

Standard library modules:
- `httpclient` - HTTP requests to OpenRouter API
- `json` - JSON handling
- `os`, `osproc` - Process and file system operations
- `terminal` - Terminal color output
- `strutils`, `sequtils` - String/sequence utilities
- `tables` - Hash tables
- `times` - Time handling
- `re` - Regular expressions

## Migration Notes (Python → Nim)

Key differences from the Python version:

1. **No virtual environment**: Nim compiles to native binary, no venv needed
2. **Static typing**: All types explicitly declared
3. **Manual memory management**: ref objects for shared state
4. **Different stdlib**: Using Nim's standard library equivalents
5. **Compiled**: Much faster execution than Python

### API Compatibility
The tool interface and prompt files remain unchanged, ensuring compatibility with existing:
- Prompt files in `prompts/`
- AGENTS.md structure
- Session/worktree layout
- Log file format

### Performance Benefits
- Faster startup (native binary vs Python interpreter)
- Lower memory usage
- Better CPU utilization
- Smaller distribution size
