# MACA - Coding Assistant

MACA is an *experimental* LLM coding assistant focused on efficient context management and convenient safety.

## Key Experimental Features

### Context Management
- File contents are shown to the LLM only once with ephemeral cache, for editing or analysis, and then replaced with a summary

### Code Map
- On session start, a project overview is added to the context
- It lists source files and their structure (classes, functions, which functions call which, line numbers)
- This should help make very targeted requests for (parts of) files to be read

### State Tracking with Diffs
- Project code map and AGENTS.md are loaded initially
- After each tool call that would change these, diffs are added to context
- When the cumulative diffs start to grow large, the entire message chain is rewritten with fresh baselines
- Balances token context minimization with context caching

### Git Worktree Isolation
- Each session gets its own worktree at `.maca/<session_id>/tree/`
- Automatic git commit after every tool call
- When complete, commits are squashed and rebased onto main
- Original commit chain preserved in a separate `maca/<feature-name>` branch

### Containerized Execution
Shell commands run in Podman/Docker containers:
- LLM chooses the base image (default: `debian:stable`)
- LLM specifies image setup `RUN` commands (for package installs etc)
- No approval needed - runs automatically with worktree mounted
- Isolated, reproducible environment

### Model Selection
- The LLM can invoke `process_files` with a different model for batch operations, enabling use of cheaper/faster models for mechanical changes or larger models for complex analysis.

## Setup

```bash
export OPENROUTER_API_KEY="your-key-here"
./maca                    # Interactive mode
./maca "your task here"   # Direct task
```

MACA auto-creates a virtual environment at `~/.cache/maca-venv-1` with required dependencies.
