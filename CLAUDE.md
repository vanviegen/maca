# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MACA (Multi-Agent Coding Assistant) is a Python-based multi-agent system that orchestrates specialized AI contexts to accomplish coding tasks. It uses git worktrees for isolation, OpenRouter API for LLM access, and Docker/Podman for safe command execution.

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

### Multi-Agent System
MACA orchestrates specialized contexts that communicate through tool calls:

**Main Context** (`contexts.Context` / `prompts/_main.md`)
- Orchestrates the entire workflow
- Delegates to specialized subcontexts
- Can work directly for simple tasks
- Manages the `.scratch/PLAN.md` file for complex tasks
- Has access to ALL tools (both main-specific like ask_user_questions and subcontext tools)

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

**Git Worktree Isolation** (`git_ops.py`)
- Each session gets isolated worktree at `.maca/<session_id>/tree/`
- Session branch: `maca-<session_id>`
- On merge: squash commits, preserve original chain in `maca/<descriptive-name>` branch
- `.scratch/` directory for temporary files (git-ignored, never committed)

**Tool System** (`tools.py`)
- Reflection-based schema generation from Python functions
- Single `_TOOLS` registry for all tools
- `@tool` decorator registers tools (no arguments needed)
- Tools listed in prompt file headers determine which tools a context can use
- Subcontext tools automatically get `rationale` parameter added to schema
- All file paths are relative to worktree

**Context Management** (`contexts.py`)
- Single `Context` class for all context types
- Each context loads system prompts from `prompts/*.md`
- Prompt files have metadata headers (default_model, tools) separated by blank line
- Context type determined by which prompt file is loaded
- Default model is `openai/gpt-5-mini` unless overridden by prompt file or at instantiation
- Contexts track and report git HEAD changes between invocations
- `AGENTS.md` loaded as system message, updates appended as diffs
- OpenRouter API used for all LLM calls

**Session Logging** (`logger.py`)
- Human-readable logs per context in `.maca/<session_id>/<context_id>.log`
- HEREDOC format for multiline values
- Tracks: LLM calls, tool invocations, tokens, costs, git changes

**Docker Execution** (`docker_ops.py`)
- Shell commands run in Docker/Podman containers
- Default: `debian:stable` with build-essential, git, python3
- Worktree mounted into container for file access
- Auto-detects docker/podman at runtime

### Critical Patterns

**AGENTS.md File**
- Documents project structure, architecture, dependencies
- Created by `code_analysis` context if missing
- Updated by `implementation` when structural changes occur
- Keep lean and focused - only essential information
- All contexts receive it as system message + diff updates

**.scratch/PLAN.md**
- Main context creates for complex multi-phase tasks
- Tracks phases, dependencies, and status
- Updated as work progresses
- Never committed to git

**Model Selection Tiers**
- `cheap`: `qwen/qwen3-coder-30b-a3b-instruct` - mechanical changes, simple tasks
- `intermediate`: `moonshotai/kimi-linear-48b-a3b-instruct` - moderate complexity
- `default`: `anthropic/claude-sonnet-4.5` - complex tasks
- `expensive`: `anthropic/claude-opus-4.1` - ask user first

**Tool Call Efficiency**
- Batch operations: read multiple files in ONE call
- Use glob patterns with arrays to match multiple file types: `["**/*.py", "**/*.md"]`
- Tools support include/exclude parameters for flexible file filtering
- Subcontexts target 5-10 tool calls total
- Keep communication brief and succinct (tokens are expensive)

### Important Files

**Core Python Modules**
- `maca.py` - Main orchestration loop and MACA class
- `contexts.py` - Context classes and LLM interaction
- `tools.py` - Tool system with reflection-based schemas
- `git_ops.py` - Git worktree and branch management
- `logger.py` - Human-readable session logs
- `docker_ops.py` - Container execution for shell commands

**System Prompts** (in `prompts/`)
- `common.md` - Shared across all contexts
- `_main.md` - Main orchestrator instructions (special context)
- `implementation.md` - Implementation agent guidelines
- `code_analysis.md` - Code analysis instructions
- `research.md` - Research agent guidelines
- `review.md` - Code review guidelines
- `merge.md` - Merge conflict resolution
- `_file_processor.md` - One-shot file processing (special context)

Each prompt file starts with metadata headers:
```markdown
default_model: anthropic/claude-sonnet-4.5
tools: read_files, list_files, update_files, search, shell, subcontext_complete

Your role in the multi-agent system is: ...
```

**Entry Points**
- `maca` - Shell wrapper that creates venv and runs `maca.py`
- `maca.py` - Python entry point with argparse

## Working with MACA

### Understanding the Flow
1. User provides task → Main context plans approach
2. Main creates `.scratch/PLAN.md` for complex tasks
3. Main delegates to specialized subcontexts (auto-named: `research1`, `implementation2`, etc.)
4. Subcontexts execute tools, each tool call creates git commit
5. Main receives summaries (tokens, tool used, rationale, git diff stats)
6. Main updates PLAN.md and continues or delegates next phase
7. When complete, Main calls `main_complete()` → user approves → squash merge to main
8. Subcontexts call `subcontext_complete()` to signal their task is done

### Key Design Principles
- **Isolation**: Each session in separate worktree/branch
- **Specialization**: Different contexts for different tasks
- **Efficiency**: Batch operations, minimize tool calls, brief communication
- **Traceability**: Git commits per tool, human-readable logs
- **Safety**: Docker for command execution
- **Autonomy**: Contexts work independently with minimal back-and-forth

### Testing and Debugging
- Use `/verbose on` command at MACA prompt to see full LLM prompts/responses
- Session logs in `.maca/<session_id>/<context>.log`
- Git history shows each tool's changes
- `.scratch/` for temporary analysis/debugging files

## Code Modification Guidelines

### Adding New Tools
1. Define function in `tools.py` with proper type hints
2. Add comprehensive docstring (generates schema description)
3. Decorate with `@tool` (no arguments)
4. Schema auto-generated via reflection
5. Add tool name to `tools:` header in relevant prompt files
6. Rationale parameter automatically added for non-main contexts

### Adding New Context Types
1. Create prompt file in `prompts/<type>.md`
2. Add metadata headers:
   ```markdown
   default_model: anthropic/claude-sonnet-4.5
   tools: tool1, tool2, tool3

   Your role description...
   ```
3. Update `_main.md` prompt to document the new context type
4. Special contexts (not creatable via `create_subcontext`) should be prefixed with `_`

### Modifying Prompts
- All prompt files must start with metadata headers (`default_model:`, `tools:`) followed by blank line
- Changes to `prompts/*.md` affect all new contexts
- Keep prompts focused and actionable
- Remember: brevity is critical for inter-agent communication
- When changing tools available to a context, update the `tools:` header

### Git Operations
- Always use functions from `git_ops.py`
- Never manually manipulate `.maca/` directory
- Worktree paths returned by `create_session_worktree()`
- All commits exclude `.scratch/` and `.maca/` directories
