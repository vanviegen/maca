# MACA - Multi-Agent Coding Assistant

A high-performance multi-agent system written in Nim that orchestrates specialized AI contexts to accomplish coding tasks.

## Features

- **Multi-agent orchestration**: Specialized contexts for code analysis, research, implementation, review, and more
- **Git worktree isolation**: Each session runs in an isolated worktree with automatic commit tracking
- **Docker/Podman integration**: Safe command execution in containerized environments
- **OpenRouter API**: Access to multiple LLM providers through a single interface
- **Performance**: Native compiled binary - fast startup, low memory usage
- **Single binary**: No runtime dependencies or virtual environments needed

## Installation

### Prerequisites

1. **Nim** (>= 2.0.0)
   ```bash
   # Using choosenim (recommended)
   curl https://nim-lang.org/choosenim/init.sh -sSf | sh
   choosenim stable

   # Or download from https://nim-lang.org/install.html
   ```

2. **Git** (for worktree operations)
3. **Docker or Podman** (for safe command execution)

### Building

```bash
# Clone the repository
git clone https://github.com/vanviegen/maca.git
cd maca

# Build the binary
chmod +x build.sh
./build.sh

# The binary will be created at ./maca
```

Alternatively, build manually:
```bash
nim c -d:release --opt:speed -o:maca src/maca.nim
```

## Quick Start

```bash
# Set your OpenRouter API key
export OPENROUTER_API_KEY="your-key-here"

# Run interactively
./maca

# Run with a task
./maca "implement a new feature X"

# Specify a model
./maca -m openai/gpt-4 "analyze this codebase"

# Show help
./maca --help
```

## Architecture

### Multi-Agent System

MACA orchestrates specialized contexts that communicate through tool calls:

**Main Context** (`_main`)
- Orchestrates the entire workflow
- Delegates to specialized subcontexts
- Manages complex multi-phase tasks
- Has access to all tools

**Specialized Subcontexts**
- `code_analysis`: Analyzes codebases, maintains AGENTS.md
- `research`: Gathers information, web search
- `implementation`: Writes and modifies code
- `review`: Reviews code for quality
- `merge`: Resolves git merge conflicts

### Key Components

- **Git Worktree Isolation**: Each session gets an isolated worktree
- **Tool System**: Explicit schema definitions with JSON schemas
- **Context Management**: Tracks AGENTS.md and git HEAD changes
- **Session Logging**: Human-readable logs in `.maca/<session>/`
- **Docker Execution**: Commands run in containers for safety

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
├── prompts/                  # System prompts for contexts
├── maca.nimble              # Nimble package file
├── build.sh                 # Build script
└── README.md                # This file
```

## Development

### Building for Development

```bash
# Compile without optimizations (faster build)
nim c src/maca.nim

# Build with debug symbols
nim c --debugger:native -o:maca_debug src/maca.nim

# Run directly without building binary
nim c -r src/maca.nim
```

### Code Style

- `camelCase` for procs and variables
- `PascalCase` for types
- `snake_case` for tool names (API compatibility)
- 2-space indentation
- Module-level documentation comments

## Performance

Compared to interpreted languages:

| Aspect | Python | Nim |
|--------|--------|-----|
| Startup | ~100ms | ~1ms |
| Memory | ~50MB base | ~5MB base |
| Distribution | 10MB+ with deps | 1-2MB single binary |
| Runtime | Interpreted | Native compiled |

## Configuration

### Environment Variables

- `OPENROUTER_API_KEY`: Your OpenRouter API key (required)

### Model Selection

Models are configured in prompt files or via CLI:
- Default: `openai/gpt-5-mini`
- Alternative: `anthropic/claude-sonnet-4.5`
- Fast/cheap: `qwen/qwen3-coder-30b-a3b-instruct`

## Documentation

- **CLAUDE_NIM.md**: Detailed architecture and development guide
- **Prompt files**: See `prompts/` directory for system prompts
- **Nim language**: https://nim-lang.org/documentation.html

## Troubleshooting

### Build Errors

If you encounter build errors:

1. Check Nim version: `nim --version` (should be >= 2.0.0)
2. Update nimble packages: `nimble refresh && nimble install`
3. Clean and rebuild: `rm -f maca && ./build.sh`

### Runtime Issues

- **"OPENROUTER_API_KEY not set"**: Export the environment variable
- **Git errors**: Ensure you're in a git repository or let MACA initialize one
- **Docker errors**: Verify Docker/Podman is installed and running

## Contributing

Contributions are welcome! Please:

1. Follow Nim best practices and idioms
2. Maintain API compatibility with prompt files
3. Update documentation for any changes
4. Test with existing prompt files

## License

MIT License - see LICENSE file for details

## Credits

- Original Python implementation by MACA contributors
- Nim conversion maintaining full API compatibility
- Built with Nim and powered by OpenRouter

## Links

- Repository: https://github.com/vanviegen/maca
- Nim Language: https://nim-lang.org
- OpenRouter: https://openrouter.ai
