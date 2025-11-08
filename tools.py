#!/usr/bin/env python3
"""Tool system with reflection-based schema generation."""

import inspect
import re
import glob as glob_module
from pathlib import Path
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union
import typing

# Global variables set by the orchestrator
WORKTREE_PATH = None
REPO_ROOT = None


# Tool registry
_MAIN_TOOLS = {}
_SUBCONTEXT_TOOLS = {}


def tool(context_type='subcontext'):
    """
    Decorator to register a function as an LLM tool.

    Args:
        context_type: 'main' or 'subcontext' to determine which context can use this tool
    """
    def decorator(func):
        # Generate schema from function
        schema = generate_tool_schema(func, context_type)

        # Register tool
        if context_type == 'main':
            _MAIN_TOOLS[func.__name__] = {
                'function': func,
                'schema': schema
            }
        else:
            _SUBCONTEXT_TOOLS[func.__name__] = {
                'function': func,
                'schema': schema
            }

        return func
    return decorator


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


def generate_tool_schema(func, context_type='subcontext') -> Dict:
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

    # Add automatic rationale parameter only for subcontext tools
    if context_type == 'subcontext':
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


def get_tool_schemas(context_type='subcontext') -> List[Dict]:
    """Get all tool schemas for a given context type."""
    tools = _MAIN_TOOLS if context_type == 'main' else _SUBCONTEXT_TOOLS
    return [tool_info['schema'] for tool_info in tools.values()]


def execute_tool(tool_name: str, arguments: Dict, context_type='subcontext') -> Any:
    """Execute a tool with the given arguments."""
    tools = _MAIN_TOOLS if context_type == 'main' else _SUBCONTEXT_TOOLS

    if tool_name not in tools:
        raise ValueError(f"Unknown tool: {tool_name}")

    func = tools[tool_name]['function']

    # Remove rationale from arguments before calling (it's just for logging)
    exec_args = {k: v for k, v in arguments.items() if k != 'rationale'}

    return func(**exec_args)


# ==============================================================================
# SUBCONTEXT TOOLS
# ==============================================================================

@tool('subcontext')
def read_files(file_paths: List[str], start_line: int = 1, max_lines: int = 100) -> List[Dict[str, Any]]:
    """
    Read one or more files, optionally with line range limits.

    Args:
        file_paths: List of file paths to read
        start_line: Line number to start reading from (1-indexed)
        max_lines: Maximum number of lines to read per file

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


@tool('subcontext')
def list_files(glob_pattern: str = "**/*", max_files: int = 200) -> List[str]:
    """
    List files in the worktree matching a glob pattern.

    Args:
        glob_pattern: Glob pattern to match files (e.g., "**/*.py", "src/**/*.js")
        max_files: Maximum number of files to return

    Returns:
        List of file paths relative to worktree root
    """
    worktree = Path(WORKTREE_PATH)
    matches = []

    for path in worktree.glob(glob_pattern):
        if path.is_file():
            rel_path = path.relative_to(worktree)
            matches.append(str(rel_path))

            if len(matches) >= max_files:
                break

    return sorted(matches)


@tool('subcontext')
def update_files(updates: List[Dict[str, str]]) -> None:
    """
    Update one or more files with new content.

    Each update can either:
    1. Write entire file: {"file_path": "path/to/file", "data": "new content"}
    2. Search and replace: {"file_path": "path/to/file", "old_data": "search", "new_data": "replacement", "allow_multiple": false}

    Args:
        updates: List of update specifications
    """
    from . import WORKTREE_PATH

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


@tool('subcontext')
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


@tool('subcontext')
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


@tool('subcontext')
def complete(result: str) -> None:
    """
    Signal that the task is complete and return the result to the main context.

    Args:
        result: Summary of what was accomplished
    """
    # This is handled specially by the context runner
    pass


# ==============================================================================
# MAIN CONTEXT TOOLS
# ==============================================================================

@tool('main')
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
    from prompt_toolkit.history import FileHistory
    from pathlib import Path

    # Use shared history file
    history_file = Path.home() / '.aai' / 'history'
    history_file.parent.mkdir(exist_ok=True)
    history = FileHistory(str(history_file))

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
            return pt_prompt(f"{prompt}\n> ", history=history)
        return result
    else:
        # Simple text input
        return pt_prompt(f"{prompt}\n> ", history=history)


@tool('main')
def create_subcontext(unique_name: str, context_type: str, task: str, model: str = "auto", max_response_chars: int = 2000) -> str:
    """
    Create a new subcontext to work on a specific task.

    Args:
        unique_name: Unique identifier for this subcontext
        context_type: Type of context (code_analysis, research, implementation, review, merge)
        task: Description of the task for this subcontext
        model: Model to use ("auto" to let system choose, or specific model name)
        max_response_chars: Maximum characters the subcontext can return

    Returns:
        Confirmation message
    """
    # Handled by the orchestrator
    return f"Created subcontext '{unique_name}' of type '{context_type}'"


@tool('main')
def continue_subcontext(unique_name: str, guidance: str = "") -> str:
    """
    Continue running an existing subcontext, optionally with additional guidance.

    Args:
        unique_name: The unique identifier of the subcontext to continue
        guidance: Optional additional guidance or feedback for the subcontext

    Returns:
        Confirmation message
    """
    # Handled by the orchestrator
    return f"Continuing subcontext '{unique_name}'"


@tool('main')
def complete(result: str) -> None:
    """
    Signal that the entire task is complete.

    Args:
        result: Summary of what was accomplished
    """
    # Handled by the orchestrator
    pass
