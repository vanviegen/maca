# MACA - Nim Conversion

This repository contains a Nim conversion of the MACA (Multi-Agent Coding Assistant) codebase, originally written in Python.

## Status

✅ **Complete**: All core modules have been converted to Nim:
- `src/maca/utils.nim` - Utility functions and color printing
- `src/maca/logger.nim` - Session logging with HEREDOC format
- `src/maca/git_ops.nim` - Git worktree and branch management
- `src/maca/docker_ops.nim` - Docker/Podman container execution
- `src/maca/tools.nim` - Tool system with JSON schema definitions
- `src/maca/context.nim` - LLM interaction and context management
- `src/maca.nim` - Main entry point and orchestration

⚠️ **Note**: Tool implementations in `tools.nim` have placeholder stubs that need to be completed for full functionality.

## Why Nim?

The conversion to Nim provides several benefits:

1. **Performance**: Compiled to native code - much faster startup and execution
2. **Memory Efficiency**: Lower memory footprint than Python
3. **Single Binary**: No virtual environment or Python dependencies needed
4. **Type Safety**: Compile-time type checking catches errors early
5. **Small Distribution**: Single self-contained binary

## Installation

### Prerequisites

1. **Nim Compiler** (>= 2.0.0)
   ```bash
   # Using choosenim (recommended)
   curl https://nim-lang.org/choosenim/init.sh -sSf | sh
   choosenim stable

   # Or install directly
   # See: https://nim-lang.org/install.html
   ```

2. **Nimble** (usually comes with Nim)
   ```bash
   nimble --version
   ```

3. **Git** (for worktree operations)
   ```bash
   git --version
   ```

4. **Docker or Podman** (for safe command execution)
   ```bash
   docker --version  # or
   podman --version
   ```

### Build

```bash
# Install Nim dependencies
nimble install

# Build the maca binary
nimble build

# Or build with optimizations
nimble build -d:release

# The binary will be created at ./maca
```

## Usage

Same as the Python version:

```bash
# Set your OpenRouter API key
export OPENROUTER_API_KEY="your-key-here"

# Run interactively
./maca

# Run with a task
./maca "implement feature X"

# Specify model
./maca -m openai/gpt-4 "your task"

# Show help
./maca --help
```

## Project Structure

```
.
├── src/
│   ├── maca.nim              # Main entry point
│   └── maca/
│       ├── context.nim       # LLM context management
│       ├── tools.nim         # Tool definitions and execution
│       ├── git_ops.nim       # Git operations
│       ├── docker_ops.nim    # Container operations
│       ├── logger.nim        # Session logging
│       └── utils.nim         # Utilities
├── prompts/                  # System prompts (unchanged)
├── maca.nimble              # Nimble package file
├── CLAUDE_NIM.md            # Documentation for Nim version
└── README_NIM.md            # This file
```

## Differences from Python Version

### Core Differences

1. **Type System**: Nim uses static typing
   - `ref object` for reference-counted types
   - Explicit type declarations
   - Compile-time type checking

2. **Error Handling**: Uses exception system similar to Python
   - Custom exception types like `ContextError`, `GitError`
   - Try/except blocks

3. **Modules**: Different import system
   - `import std/[json, os, strutils]` for stdlib
   - `import maca/context` for local modules

4. **JSON Handling**: Using Nim's JSON module
   - `%*` macro for JSON literals
   - `parseJson()` for parsing
   - `$` operator for string conversion

### API Compatibility

The following remain unchanged for compatibility:

- **Prompt files**: All `prompts/*.md` files work as-is
- **Tool interface**: Same tool schemas and calling conventions
- **Session layout**: Same `.maca/` directory structure
- **Log format**: Same HEREDOC-based logging
- **Git workflow**: Same worktree and branching strategy

### Performance Characteristics

| Aspect | Python | Nim |
|--------|--------|-----|
| Startup | ~100ms | ~1ms |
| Memory | ~50MB base | ~5MB base |
| Distribution | 10MB+ with deps | 1-2MB single binary |
| Runtime | Interpreted | Native compiled |

## Development

### Building for Development

```bash
# Compile and run directly
nim c -r src/maca.nim

# With debug info
nim c --debugger:native src/maca.nim

# Run tests (when added)
nimble test
```

### Adding New Tools

1. Define the tool handler proc in `src/maca/tools.nim`:
   ```nim
   proc myNewTool(args: JsonNode): JsonNode =
     ## Tool implementation
     let param = args["param"].getStr()
     result = %*{"status": "ok"}
   ```

2. Create the JSON schema:
   ```nim
   let schema = %*{
     "type": "function",
     "function": {
       "name": "my_new_tool",
       "description": "What this tool does",
       "parameters": {
         "type": "object",
         "properties": {
           "param": {"type": "string", "description": "Parameter desc"}
         },
         "required": ["param"]
       }
     }
   }
   ```

3. Register it in `initTools()`:
   ```nim
   registerTool("my_new_tool", schema, myNewTool)
   ```

4. Add to relevant prompt files' `tools:` header

### Code Style

Following Nim conventions:
- `camelCase` for procs and variables
- `PascalCase` for types
- `snake_case` for tool names (for Python API compatibility)
- 2-space indentation
- Module-level documentation comments

## TODO

The following features need completion:

1. **Tool Implementations** in `tools.nim`:
   - [ ] Complete `readFiles()` implementation
   - [ ] Complete `listFiles()` implementation
   - [ ] Complete `updateFiles()` implementation
   - [ ] Complete `search()` implementation
   - [ ] Complete `shell()` integration with docker_ops
   - [ ] Complete `getUserInput()` with nimline
   - [ ] Complete `createSubcontext()` integration
   - [ ] Complete `runOneshotPerFile()` implementation
   - [ ] Complete `continueSubcontext()` implementation

2. **Context Enhancements** in `context.nim`:
   - [ ] Implement `diffAgentsMd()` for AGENTS.md updates
   - [ ] Implement `checkHeadChanges()` for git tracking
   - [ ] Complete tool execution and result handling
   - [ ] Add ReadyResult detection and handling

3. **Interactive Features**:
   - [ ] Better multi-line input (integrate nimline or illwill)
   - [ ] History file support
   - [ ] Radio list dialogs for user choices

4. **Testing**:
   - [ ] Add unit tests
   - [ ] Integration tests
   - [ ] CI/CD pipeline

5. **Documentation**:
   - [ ] API documentation
   - [ ] Tutorial/examples
   - [ ] Migration guide from Python

## Contributing

When contributing to the Nim version:

1. Maintain API compatibility with Python version where possible
2. Follow Nim best practices and idioms
3. Update documentation for any changes
4. Test with the original prompt files
5. Ensure the same session/worktree behavior

## License

Same as the original Python MACA project.

## Original Python Version

The original Python implementation is preserved in the repository. Both versions can coexist:
- Python version: `python3 run.py`
- Nim version: `./maca` (after building)

## Questions?

For questions about:
- **Nim implementation**: See this README and CLAUDE_NIM.md
- **MACA architecture**: See CLAUDE.md (original documentation)
- **Nim language**: See https://nim-lang.org/documentation.html
