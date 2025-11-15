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
./maca -n "task"          # Run in non-interactive mode (auto-answers questions, auto-merges)
```

### Non-Interactive Mode
Non-interactive mode (`-n` or `--non-interactive`) is designed for automated testing and CI/CD pipelines:

- **Requirements**: Must be used with a task argument
- **Behavior**:
  - Any user questions are auto-answered with: "This agent is running non-interactively. Please try to take a guess at the answer yourself, but be a bit conservative and refuse the assignment if needed."
  - On completion, changes are automatically merged to main branch and program exits
  - No user prompts or confirmations
- **Usage**: `./maca -n "your task here"`
- **Testing**: Always test by calling `./maca` (not `maca.py` directly) to ensure proper environment setup

### Verbose Mode
Enable verbose logging to see all log entries in real-time:

- **Usage**: `./maca -v` or `./maca --verbose`
- Can be combined with other flags: `./maca -n -v "task"`
- Can also be toggled during interactive sessions with `/verbose on` or `/verbose off`

### Testing New Functionality
When implementing new features, test them if possible using:
```bash
./maca -n "task description"
# Optionally add -v for verbose output to debug:
./maca -n -v "task description"
```

If testing requires user interaction and cannot be automated, output a single sentence about manual testing requirements instead.

### Environment Setup
MACA auto-creates a virtual environment at `~/.cache/maca-venv-1` with required dependencies (prompt-toolkit).

Set the OpenRouter API key:
```bash
export OPENROUTER_API_KEY="your-key-here"
```

## High-Level Architecture

### Single-Tool System
MACA uses a single-tool architecture: the AI agent calls only one tool (`respond`) with various parameters for different actions.

### Key Components

**Git Worktree Isolation** (`git_ops.py`)
- Each session gets isolated worktree at `.maca/<session_id>/tree/`
- Session branch: `maca/<session_id>`
- On merge: squash commits, preserve original chain in `maca/<descriptive-name>` branch
- `.scratch/` directory for temporary files (git-ignored, never committed)

**Tool System** (`tools.py`)
- Single `respond` tool with multiple parameters
- Reflection-based schema generation from Python functions
- `@tool` decorator registers the tool
- Tool returns tuple: (immediate_result, context_summary)
- All file paths are relative to worktree

**Single Tool: respond**
```python
respond(
    think_out_loud: str,                    # Brief reasoning
    result_text: str,                       # What to report back
    file_updates: Optional[List[FileUpdate]],     # File modifications
    processors: Optional[List[Processor]],         # Data gathering sub-contexts
    user_questions: Optional[List[Question]],      # Ask user for input
    complete: bool = False                  # Mark task as complete
)
```

**Context Management** (`maca.py`)
- Single `MACA` class for the coding assistant
- Loads system prompt from `prompt.md` (plain markdown, no headers)
- Model specified via -m command line argument (default: anthropic/claude-sonnet-4.5)
- OpenRouter API used for all LLM calls (via `call_llm` in utils.py)
- Two-tier context system:
  - **Temporary context**: Full data (file contents, search results, shell output, complete file_updates)
  - **Long-term context**: Metadata and summaries only; large data replaced with "OMITTED"
  - Tool results shown once with full data (ephemeral cache), then only metadata persists
  - Uses Anthropic ephemeral cache control markers
  - LLM sees full data once per respond call, extracts key info to `notes_for_context`

**State Tracking** (AGENTS.md and code_map)
- AGENTS.md and project code_map loaded at initialization as system messages
- After each respond call that creates a git commit, both are regenerated
- If either changed, a diff is added as a system message
- When state changes exceed 25% of original size, history is rewritten: all diffs removed and replaced with new baselines
- This balances token caching (changes are visible) with context efficiency

**Session Logging** (`logger.py`)
- Human-readable logs in `.maca/<session_id>.log`
- HEREDOC format for multiline values
- Tracks: LLM calls, tool invocations, tokens, costs, git changes

**Docker Execution** (`docker_ops.py`)
- Shell commands run in Docker/Podman containers via processors
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
- File operations support `exclude_files` parameter in processors
- Defaults to `['.gitignore']` to respect gitignore patterns
- Uses `GitignoreMatcher` in utils.py for gitignore semantics (negation, directory patterns, etc.)

**file_updates Parameter**
- Create, modify, or delete files
- **Executed FIRST** (before processors and data gathering operations)
- Operations: `overwrite` (full file write), `update` (search/replace), `rename` (move/delete)
- Each update requires a `summary` field for context tracking
- Operations execute in order: overwrite, update, rename
- Full update details (overwrite content, search/replace ops) shown in temporary context only
- Long-term context stores: status, summary, count, and paths (content marked as "OMITTED")

**processors Parameter**
- Spawn sub-contexts for data gathering (reading files, shell commands, searches)
- Each processor gets its own LLM call with specialized prompt (SUBPROMPT.md)
- Model size selection: tiny, small, medium, large, huge
- Processors can:
  - Read files (`read_files`)
  - Execute shell commands (`shell_commands`)
  - Search file contents (`file_searches`)
  - Make file updates (`file_updates` in processor's respond call)
- Processors return `result_text` which is added to main context
- File contents shown to processor only, never persist in main context

**user_questions Parameter**
- Ask user for input when clarification needed
- Support preset answers for better UX
- Multiple questions can be asked in one call

**complete Parameter**
- Set to `true` when task is fully complete
- Triggers user review and merge workflow
- User can merge, continue, cancel, or delete

**respond Tool Return Values**
- Returns tuple: `(long_term_response, temporary_response, done)`
- Both responses are Dicts with same structure but different content:
  - **temporary_response**: Full data including file contents, search results, complete file_updates
  - **long_term_response**: Metadata only; large data replaced with "OMITTED"
- Both responses serialized to JSON via `json.dumps()` in `maca.py`
- Temporary response added with ephemeral cache control (cleared after next respond call)
- Long-term response persists across all iterations

**"OMITTED" Replacement Strategy**
- String `"OMITTED"` replaces large data in long-term context
- Applies to:
  - `file_updates`: Full update objects → count + paths only
  - `file_reads`: File contents → "OMITTED" string
  - `file_searches`: Search results → "OMITTED" string
  - `shell_commands`: Command output → "OMITTED" string
  - `sub_processors`: Processor results → "OMITTED" string
  - `user_questions`: Question details → "OMITTED" string (Q&A stored as separate messages)
- Metadata always preserved: counts, paths, specs, commands, summaries

**LLM Call Logic** (`utils.py`)
- `call_llm()` function handles all LLM API interactions
- Used by both `maca.py` main loop and processor execution
- Handles: API call, error handling, logging, usage tracking
- Returns dict with message, cost, and usage

### Important Files

**Core Python Modules**
- `maca.py` - Main orchestration loop and MACA class
- `tools.py` - Single respond tool with helper functions
- `git_ops.py` - Git worktree and branch management
- `logger.py` - Human-readable session logs
- `docker_ops.py` - Container execution for shell commands
- `utils.py` - Utilities: cprint, gitignore parsing, LLM call logic, Color dataclass
- `code_map.py` - Code structure extraction and file listing

**System Prompts**
- `prompt.md` - Main system prompt for the assistant
- `SUBPROMPT.md` - Specialized prompt for processors

**Entry Points**
- `maca` - Shell wrapper that creates venv and runs `maca.py`
- `maca.py` - Python entry point with argparse

## Working with MACA

### Understanding the Flow
1. User provides task
2. Assistant calls `respond` tool with appropriate parameters
3. **Execution order in respond tool:**
   a. `file_updates` executed FIRST (files modified, LLM already output write comments)
   b. Git commit created if files were modified
   c. AGENTS.md and code_map diffs tracked (if changed after commit)
   d. `user_questions` asked (to get input before data gathering)
   e. `file_reads` executed (file contents read into temporary context)
   f. `file_searches` executed (search results into temporary context)
   g. `shell_commands` executed (command output into temporary context)
   h. `sub_processors` executed (processor results into temporary context)
   i. `notes_for_context` and `user_output` added to responses
4. Tool returns `(long_term_response, temporary_response, done)`
5. Both responses serialized to JSON and added to context:
   - Temporary: full data with ephemeral cache (cleared next iteration)
   - Long-term: metadata with "OMITTED" for large data (persists)
6. When complete, assistant sets `done=true` → user approves → squash merge to main

### Key Design Principles
- **Single Tool**: All actions through one `respond` tool with different parameters
- **Isolation**: Each session in separate worktree/branch
- **Efficiency**: Batch operations in one respond call
- **Traceability**: Git commits per respond call, human-readable logs, state diffs
- **Safety**: Docker for command execution
- **Autonomy**: Works independently with minimal back-and-forth
- **Context Management**: Two-tier system keeps full data ephemeral, metadata persists

### Testing and Debugging
- Session logs in `.maca/<session_id>.log`
- Git history shows each respond call's changes
- `.scratch/` for temporary analysis/debugging files

## Code Modification Guidelines

### Modifying the Tool
- The `respond` tool is defined in `tools.py`
- Parameters are defined as TypedDict classes
- Helper functions handle specific operations (apply_file_updates, execute_processor, etc.)
- Tool returns tuple: `(long_term_response, temporary_response, done)`
  - Both responses are Dicts with mirrored structure
  - Long-term response replaces large data with "OMITTED" string
  - Temporary response contains full data
- Schema auto-generated via reflection

### Adding New Processor Capabilities
1. Define new TypedDict for the data structure
2. Add parameter to Processor TypedDict
3. Implement execution logic in `execute_processor()`
4. Update SUBPROMPT.md to document the capability

### Modifying the Prompts
- `prompt.md` is the main system prompt (plain markdown, no headers)
- `SUBPROMPT.md` is the processor system prompt
- Changes to prompts affect new sessions
- Keep prompts focused and actionable

### Git Operations
- Always use functions from `git_ops.py`
- Never manually manipulate `.maca/` directory
- Worktree paths returned by `create_session_worktree()`
- All commits exclude `.scratch/` and `.maca/` directories

### Type Definitions

Key TypedDict classes in `tools.py`:

- **FileUpdate**: Specify file operations (overwrite/update/rename)
- **SearchReplaceOp**: Search/replace operation within a file
- **Processor**: Processor specification with model, assignment, and operations
- **FileRead**: Read file or file range
- **ShellCommand**: Execute command in container
- **FileSearch**: Search file contents with regex
- **Question**: Ask user a question with optional preset answers

## Architecture Benefits

**Single-Tool Architecture:**
- Cleaner, more intentional API
- Single "think → action → result" pattern
- Better control over context
- Easier to reason about tool usage

**Processors:**
- Replace old `process_files` tool but more general
- Support reading files, shell commands, and searches
- Keep file contents out of main context
- Model size selection for cost optimization

**Integrated Operations:**
- File updates, processors, and questions in one call
- Batch related operations efficiently
- Atomic commits per logical action
