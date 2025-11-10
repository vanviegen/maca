#!/usr/bin/env python3
"""Tool system with reflection-based schema generation."""

import inspect
import re
import os
from pathlib import Path
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union
import typing
from prompt_toolkit.history import FileHistory

# Global variables set by the orchestrator
WORKTREE_PATH = None
REPO_ROOT = None
MACA_INSTANCE = None  # Reference to the MACA orchestrator instance

# Setup shared input history
HISTORY_FILE = Path.home() / '.maca' / 'history'
HISTORY_FILE.parent.mkdir(exist_ok=True)
HISTORY = FileHistory(str(HISTORY_FILE))


# Tool registry - single registry for all tools
_TOOLS = {}


def tool(func):
    """
    Decorator to register a function as an LLM tool.

    Tools are registered by name and schemas are generated on-demand
    based on the context that uses them.
    """
    _TOOLS[func.__name__] = {
        'function': func
    }
    return func


def python_type_to_json_type(py_type) -> Dict:
    """Convert Python type hint to JSON schema type."""
    origin = get_origin(py_type)

    # Handle Optional (Union with None)
    if origin is Union:
        args = get_args(py_type)
        # Filter out None
        non_none_args = [arg for arg in args if arg is not type(None)]
        if len(non_none_args) == 1:
            # It's Optional[T], extract T
            return python_type_to_json_type(non_none_args[0])
        else:
            # Complex Union, default to string
            return {'type': 'string'}

    # Handle List
    if origin is list:
        args = get_args(py_type)
        if args:
            item_type = python_type_to_json_type(args[0])
            return {'type': 'array', 'items': item_type}
        return {'type': 'array'}

    # Handle Dict
    if origin is dict:
        return {'type': 'object'}

    # Handle basic types
    if py_type == str:
        return {'type': 'string'}
    elif py_type == int:
        return {'type': 'integer'}
    elif py_type == float:
        return {'type': 'number'}
    elif py_type == bool:
        return {'type': 'boolean'}
    elif py_type == Any:
        return {}
    else:
        # Default to string for unknown types
        return {'type': 'string'}


def generate_tool_schema(func, add_rationale: bool = False) -> Dict:
    """Generate OpenAI-compatible function schema from a Python function."""
    sig = inspect.signature(func)
    type_hints = get_type_hints(func)
    doc = inspect.getdoc(func) or ""

    # Extract description and parameter docs from docstring
    description_lines = []
    param_docs = {}

    in_args_section = False
    current_param = None

    for line in doc.split('\n'):
        line = line.strip()
        if line.lower().startswith('args:'):
            in_args_section = True
            continue
        elif line.lower().startswith('returns:'):
            in_args_section = False
            continue

        if in_args_section:
            # Parse parameter documentation
            match = re.match(r'(\w+):\s*(.*)', line)
            if match:
                current_param = match.group(1)
                param_docs[current_param] = match.group(2)
            elif current_param and line:
                param_docs[current_param] += ' ' + line
        elif not in_args_section and line:
            description_lines.append(line)

    description = ' '.join(description_lines).strip()

    # Build parameters schema
    properties = {}
    required = []

    for param_name, param in sig.parameters.items():
        if param_name == 'self' or param_name == 'rationale':
            continue

        param_schema = python_type_to_json_type(type_hints.get(param_name, str))

        # Add description if available
        if param_name in param_docs:
            param_schema['description'] = param_docs[param_name]

        properties[param_name] = param_schema

        # Check if required (no default value)
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    # Add automatic rationale parameter if requested
    if add_rationale:
        properties['rationale'] = {
            'type': 'string',
            'description': 'Explanation of why this tool is being called and what you expect to accomplish'
        }
        required.append('rationale')

    return {
        'type': 'function',
        'function': {
            'name': func.__name__,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties,
                'required': required,
                'additionalProperties': False
            }
        }
    }


def get_tool_schemas(tool_names: List[str], add_rationale: bool = False) -> List[Dict]:
    """
    Get tool schemas for the specified tool names.

    Args:
        tool_names: List of tool names to generate schemas for
        add_rationale: Whether to add rationale parameter to all tools

    Returns:
        List of tool schemas
    """
    schemas = []
    for tool_name in tool_names:
        if tool_name not in _TOOLS:
            raise ValueError(f"Unknown tool: {tool_name}")

        tool_info = _TOOLS[tool_name]
        schema = generate_tool_schema(tool_info['function'], add_rationale=add_rationale)
        schemas.append(schema)

    return schemas


def execute_tool(tool_name: str, arguments: Dict) -> Any:
    """Execute a tool with the given arguments."""
    if tool_name not in _TOOLS:
        raise ValueError(f"Unknown tool: {tool_name}")

    tool_info = _TOOLS[tool_name]
    func = tool_info['function']

    # Remove rationale from arguments before calling (it's just for logging)
    exec_args = {k: v for k, v in arguments.items() if k != 'rationale'}

    return func(**exec_args)


# ==============================================================================
# TOOLS
# ==============================================================================

@tool
def read_files(file_paths: List[str], start_line: int = 1, max_lines: int = 250) -> List[Dict[str, Any]]:
    """
    Read one or more files, optionally with line range limits.

    IMPORTANT: Read ALL relevant files in a SINGLE call for efficiency. Avoid multiple tool calls.

    Args:
        file_paths: List of file paths to read
        start_line: Line number to start reading from (1-indexed)
        max_lines: Maximum number of lines to read per file (default: 250)

    Returns:
        List of dicts with file_path, data, and remaining_lines for each file
    """
    results = []
    for file_path in file_paths:
        full_path = Path(WORKTREE_PATH) / file_path

        if not full_path.exists():
            results.append({
                'file_path': file_path,
                'error': 'File not found',
                'data': '',
                'remaining_lines': 0
            })
            continue

        try:
            with open(full_path, 'r') as f:
                lines = f.readlines()

            total_lines = len(lines)
            end_line = min(start_line - 1 + max_lines, total_lines)
            selected_lines = lines[start_line - 1:end_line]

            data = ''.join(selected_lines)
            remaining = max(0, total_lines - end_line)

            results.append({
                'file_path': file_path,
                'data': data,
                'remaining_lines': remaining,
                'total_lines': total_lines
            })
        except Exception as e:
            results.append({
                'file_path': file_path,
                'error': str(e),
                'data': '',
                'remaining_lines': 0
            })

    return results


@tool
def list_files(path_regex: str = ".*", max_files: int = 50) -> Dict[str, Any]:
    """
    List files in the worktree matching a regular expression pattern.

    Returns a random sampling if more files match than max_files.
    Use this to get an impression of what files exist matching a pattern.

    Use | to match multiple file types efficiently in one call.
    Examples (as JSON strings):
    - "\\.py$" - All Python files
    - "\\.(py|js|ts)$" - Python, JavaScript, and TypeScript files
    - "^src/.*\\.(py|md)$" - Python and Markdown files in src/
    - "(test_.*\\.py|.*_test\\.py)$" - Python test files
    - "^[^/\\\\]*$" - Files in top directory only (no subdirectories)

    Args:
        path_regex: Regular expression to match file paths (applied to full relative path)
        max_files: Maximum number of files to return (default: 50)

    Returns:
        Dict with 'total_count' (int) and 'files' (list of file info objects).
        Each file object contains:
        - path: relative path string
        - bytes: file size in bytes
        - lines: number of lines (for text files, omitted for binary/large files)
        - type: "directory", "symlink", or "executable" (omitted for regular files)
        - target: symlink target (only for symlinks)
        - entries: number of entries in directory (only for directories)

        If total_count > max_files, 'files' contains random sampling.
    """
    import random

    worktree = Path(WORKTREE_PATH)
    matches = []
    pattern = re.compile(path_regex)

    def get_file_info(path: Path, rel_path_str: str) -> Dict[str, Any]:
        """Get detailed info about a file/directory."""
        info = {"path": rel_path_str}

        try:
            stat = path.lstat()  # Use lstat to not follow symlinks

            # Check if symlink
            if path.is_symlink():
                info["type"] = "symlink"
                try:
                    info["target"] = str(path.readlink())
                except:
                    info["target"] = "?"
                return info

            # Check if directory
            if path.is_dir():
                info["type"] = "directory"
                try:
                    entries = list(path.iterdir())
                    info["entries"] = len(entries)
                except:
                    info["entries"] = 0
                return info

            # Regular file - get size
            info["bytes"] = stat.st_size

            # Check if executable
            if stat.st_mode & 0o111:  # Check execute bits
                info["type"] = "executable"

            # Try to count lines for text files (skip large files)
            if stat.st_size < 1024 * 1024:  # Only for files < 1MB
                try:
                    with open(path, 'r', encoding='utf-8', errors='strict') as f:
                        info["lines"] = sum(1 for _ in f)
                except:
                    pass  # Binary or invalid encoding, skip line count

        except Exception:
            # If we can't stat the file, just return the path
            pass

        return info

    # Walk the entire tree and collect ALL matches
    for root, dirs, files in os.walk(worktree, followlinks=False):
        # Skip .git, .scratch, and other hidden directories
        dirs[:] = [d for d in dirs if not d.startswith('.')]

        # Match directories
        for dir_name in dirs:
            full_path = Path(root) / dir_name
            rel_path = full_path.relative_to(worktree)
            rel_path_str = str(rel_path)

            if pattern.search(rel_path_str):
                matches.append(get_file_info(full_path, rel_path_str))

        # Match files
        for file in files:
            full_path = Path(root) / file
            rel_path = full_path.relative_to(worktree)
            rel_path_str = str(rel_path)

            if pattern.search(rel_path_str):
                matches.append(get_file_info(full_path, rel_path_str))

    total_count = len(matches)

    # Sort by path
    matches.sort(key=lambda x: x["path"])

    # If we have more matches than max_files, return random sampling
    if total_count > max_files:
        sampled = random.sample(matches, max_files)
        sampled.sort(key=lambda x: x["path"])
        return {
            'total_count': total_count,
            'files': sampled
        }
    else:
        return {
            'total_count': total_count,
            'files': matches
        }


@tool
def update_files(updates: List[Dict[str, str]]) -> None:
    """
    Update one or more files with new content.

    Each update can either:
    1. Write entire file: {"file_path": "path/to/file", "data": "new content"}
    2. Search and replace: {"file_path": "path/to/file", "old_data": "search", "new_data": "replacement", "allow_multiple": false}

    Args:
        updates: List of update specifications
    """
    for update in updates:
        file_path = update['file_path']
        full_path = Path(WORKTREE_PATH) / file_path

        # Ensure parent directory exists
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if 'data' in update:
            # Full file write
            full_path.write_text(update['data'])
        elif 'old_data' in update and 'new_data' in update:
            # Search and replace
            if not full_path.exists():
                raise ValueError(f"Cannot search/replace in non-existent file: {file_path}")

            content = full_path.read_text()
            old_data = update['old_data']
            new_data = update['new_data']
            allow_multiple = update.get('allow_multiple', False)

            count = content.count(old_data)
            if count == 0:
                raise ValueError(f"Search string not found in {file_path}")
            elif count > 1 and not allow_multiple:
                raise ValueError(f"Search string appears {count} times in {file_path}, but allow_multiple=false")

            if allow_multiple:
                content = content.replace(old_data, new_data)
            else:
                content = content.replace(old_data, new_data, 1)

            full_path.write_text(content)
        else:
            raise ValueError(f"Invalid update specification: {update}")


@tool
def search(glob_pattern: str, regex: str, max_results: int = 10, lines_before: int = 2, lines_after: int = 2) -> List[Dict[str, Any]]:
    """
    Search for a regex pattern in files matching a glob pattern.

    Args:
        glob_pattern: Glob pattern for files to search (e.g., "**/*.py")
        regex: Regular expression to search for
        max_results: Maximum number of matches to return
        lines_before: Number of context lines before each match
        lines_after: Number of context lines after each match

    Returns:
        List of matches with file_path, line_number, and context lines
    """
    worktree = Path(WORKTREE_PATH)
    results = []
    pattern = re.compile(regex)

    for file_path in worktree.glob(glob_pattern):
        if not file_path.is_file():
            continue

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if pattern.search(line):
                    start = max(0, i - lines_before)
                    end = min(len(lines), i + lines_after + 1)
                    context = ''.join(lines[start:end])

                    results.append({
                        'file_path': str(file_path.relative_to(worktree)),
                        'line_number': i + 1,
                        'lines': context
                    })

                    if len(results) >= max_results:
                        return results
        except Exception:
            # Skip files that can't be read
            continue

    return results


@tool
def shell(command: str, docker_image: str = "debian:stable", docker_runs: List[str] = None, head: int = 50, tail: int = 50) -> Dict[str, Any]:
    """
    Execute a shell command in a Docker container.

    Args:
        command: Shell command to execute
        docker_image: Base Docker image to use
        docker_runs: List of RUN commands to execute when building the image (e.g., ["RUN apt-get update && apt-get install -y nodejs"])
        head: Number of lines to keep from start of output
        tail: Number of lines to keep from end of output

    Returns:
        Dict with stdout, stderr, and exit_code
    """
    from docker_ops import run_in_container

    if docker_runs is None:
        docker_runs = []

    return run_in_container(
        command=command,
        worktree_path=Path(WORKTREE_PATH),
        repo_root=Path(REPO_ROOT),
        docker_image=docker_image,
        docker_runs=docker_runs,
        head=head,
        tail=tail
    )


@tool
def subcontext_complete(result: str) -> None:
    """
    Signal that your subtask is complete and return the result to the main context.

    This signals that YOU (the subcontext) are done with your specific task. The main context
    may continue orchestrating other work after you complete.

    For tasks involving analysis or generating extensive output:
    - Place detailed results in files within .scratch/ directory (e.g., .scratch/analysis.md, .scratch/test-results.txt)
    - The .scratch/ directory is temporary and git-ignored - files there are never committed
    - Return a SUMMARY of findings in the result parameter
    - Mention which .scratch/ files contain detailed data if the main context asked for analysis
    - ONLY create .scratch/ files if the main context specifically requested analysis/detailed output

    For regular implementation tasks:
    - Just return a brief summary of what was done
    - Do NOT create .scratch/ files unless specifically requested

    Args:
        result: Summary of what was accomplished (and optionally mention .scratch/ files with details)
    """
    # This is handled specially by the context runner
    pass


@tool
def get_user_input(prompt: str, preset_answers: List[str] = None) -> str:
    """
    Get input from the user interactively.

    Args:
        prompt: The prompt to display to the user
        preset_answers: Optional list of preset answer choices

    Returns:
        The user's input
    """
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.shortcuts import radiolist_dialog

    if preset_answers:
        # Show radio list dialog
        choices = [(answer, answer) for answer in preset_answers]
        choices.append(('__custom__', 'Other (custom input)'))

        result = radiolist_dialog(
            title='Input Required',
            text=prompt,
            values=choices
        ).run()

        if result == '__custom__':
            return pt_prompt(f"{prompt}\n> ", history=HISTORY)
        return result
    else:
        # Simple text input
        return pt_prompt(f"{prompt}\n> ", history=HISTORY)


@tool
def create_subcontext(context_type: str, task: str, model: str = "auto", budget: int = 20000) -> str:
    """
    Create a new subcontext to work on a specific task.
    The subcontext will be automatically named (e.g., research1, implementation2, etc.)

    The subcontext will run autonomously until its budget is exhausted or it calls complete().
    Only when the budget is exceeded will control return to you for verification.
    All tool calls and rationales will still be logged to your context for monitoring.

    Available context types:
    - code_analysis: Analyze codebases, understand architecture
    - research: Gather information, look up documentation, find solutions
    - implementation: Write and modify code
    - review: Review code for quality, correctness, security
    - merge: Resolve git merge conflicts

    For processing multiple files with one-shot operations, use run_oneshot_per_file instead.

    Args:
        context_type: Type of context (code_analysis, research, implementation, review, merge)
        task: Description of the task for this subcontext
        model: Model to use ("auto" for default, or specific model name like "qwen/qwen3-coder-30b-a3b-instruct")
        budget: Maximum cost in microdollars (μ$) before returning control (default: 20000μ$ = $0.02)

    Returns:
        Confirmation message with the auto-generated name
    """
    import contexts
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import FormattedText

    maca = MACA_INSTANCE
    if not maca:
        raise ValueError("MACA instance not initialized")

    # Validate context type - reject special (underscore-prefixed) context types
    if context_type.startswith('_'):
        raise ValueError(f"Cannot create subcontext of type '{context_type}'. Types starting with '_' are reserved for special contexts.")

    # Auto-generate unique name and create subcontext
    unique_name = maca._generate_unique_context_name(context_type)
    maca._create_and_register_subcontext(unique_name, context_type, model, task)

    # Initialize budget tracking
    maca.subcontext_budgets[unique_name] = budget
    maca.subcontext_spent[unique_name] = 0

    result = f"Created {context_type} subcontext '{unique_name}' with budget {budget}μ$"
    maca.logger.log('main', type='tool_result', tool_name='create_subcontext', result=result, duration=0)

    print_formatted_text(FormattedText([
        ('', '  '),
        ('ansigreen', 'Created subcontext:'),
        ('', f' {unique_name} ({context_type}), budget: {budget}μ$'),
    ]))

    # Run the subcontext autonomously
    maca.run_subcontext(unique_name)

    return result


@tool
def run_oneshot_per_file(path_regex: str, task: str, file_limit: int = 5, model: str = "auto") -> str:
    """
    Run a one-shot file_processor on each file matching path_regex.

    Creates one file_processor instance per file. Each instance:
    - Receives the file contents and task
    - Has access to only update_files_and_complete tool
    - Terminates after calling the tool (one-shot execution)

    Useful for applying mechanical changes across multiple files, converting multiple files, or analyzing
    multiple files individually, while keeping context sizes small.

    Args:
        path_regex: Regex pattern to match files (e.g., "\\.py$" for Python files)
        task: Task description for the file_processor (applied to each file)
        file_limit: Maximum files to process (default: 5, prevents accidental bulk operations)
        model: Model to use ("auto" for default, or specific model like "qwen/qwen3-coder-30b-a3b-instruct")

    Returns:
        Confirmation message with list of files being processed
    """
    # Handled by the orchestrator
    return f"Running file_processor on files matching '{path_regex}'"


@tool
def continue_subcontext(unique_name: str, guidance: str = "", budget: int = 20000) -> str:
    """
    Continue running an existing subcontext, optionally with additional guidance.

    The subcontext will run autonomously until its budget is exhausted or it calls complete().
    Only when the budget is exceeded will control return to you for verification.
    All tool calls and rationales will still be logged to your context for monitoring.

    Args:
        unique_name: The unique identifier of the subcontext to continue
        guidance: Optional additional guidance or feedback for the subcontext
        budget: Maximum cost in microdollars (μ$) before returning control (default: 20000μ$ = $0.02)

    Returns:
        Confirmation message
    """
    from prompt_toolkit import print_formatted_text
    from prompt_toolkit.formatted_text import FormattedText

    maca = MACA_INSTANCE
    if not maca:
        raise ValueError("MACA instance not initialized")

    if unique_name not in maca.subcontexts:
        raise ValueError(f"Unknown subcontext: {unique_name}")

    # Add guidance if provided
    if guidance:
        maca.subcontexts[unique_name].add_message('user', guidance)

    # Update budget (add to existing budget)
    if unique_name not in maca.subcontext_budgets:
        maca.subcontext_budgets[unique_name] = budget
        maca.subcontext_spent[unique_name] = 0
    else:
        maca.subcontext_budgets[unique_name] += budget

    result = f"Continuing subcontext '{unique_name}' with additional budget {budget}μ$"
    maca.logger.log('main', type='tool_result', tool_name='continue_subcontext', result=result, duration=0)

    spent = maca.subcontext_spent.get(unique_name, 0)
    remaining = maca.subcontext_budgets[unique_name] - spent
    print_formatted_text(FormattedText([
        ('', '  '),
        ('ansigreen', 'Continuing subcontext:'),
        ('', f' {unique_name}, budget: {remaining}μ$ remaining'),
    ]))

    # Run the subcontext autonomously
    maca.run_subcontext(unique_name)

    return result


@tool
def main_complete(result: str) -> None:
    """
    Signal that the ENTIRE user task is complete and you're ready to end the session.

    This is different from subcontext complete() - this signals that ALL work is done,
    including any multi-phase plans, and you're ready to return control to the user.

    Only call this when:
    - All planned phases are complete
    - All subtasks have been implemented and verified
    - The user's original request has been fully satisfied
    - No further work is needed

    Args:
        result: Summary of everything that was accomplished in the entire session
    """
    # Handled by the orchestrator
    pass


@tool
def update_files_and_complete(updates: List[Dict[str, str]], result: str) -> None:
    """
    Update files and signal completion. This is the ONLY tool available to file_processor contexts.

    This is a one-shot tool - the context terminates immediately after calling it.

    Args:
        updates: List of file update specifications (same format as update_files tool)
        result: Brief summary of what was done with this file (goes to Main context)
    """
    # First perform the updates
    if updates:
        for update in updates:
            file_path = update['file_path']
            full_path = Path(WORKTREE_PATH) / file_path

            # Ensure parent directory exists
            full_path.parent.mkdir(parents=True, exist_ok=True)

            if 'data' in update:
                # Full file write
                full_path.write_text(update['data'])
            elif 'old_data' in update and 'new_data' in update:
                # Search and replace
                if not full_path.exists():
                    raise ValueError(f"Cannot search/replace in non-existent file: {file_path}")

                content = full_path.read_text()
                old_data = update['old_data']
                new_data = update['new_data']
                allow_multiple = update.get('allow_multiple', False)

                count = content.count(old_data)
                if count == 0:
                    raise ValueError(f"Search string not found in {file_path}")
                elif count > 1 and not allow_multiple:
                    raise ValueError(f"Search string appears {count} times in {file_path}, but allow_multiple=false")

                if allow_multiple:
                    content = content.replace(old_data, new_data)
                else:
                    content = content.replace(old_data, new_data, 1)

                full_path.write_text(content)
            else:
                raise ValueError(f"Invalid update specification: {update}")

    # Result is handled by the orchestrator
    pass
