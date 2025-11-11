#!/usr/bin/env python3
"""Tool system with reflection-based schema generation."""

from dataclasses import dataclass
import inspect
import re
import json
import random
from fnmatch import fnmatch
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from pathlib import Path
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union, Optional

from maca import maca
from utils import color_print
from docker_ops import run_in_container
from context import Context
import git_ops


def check_path(path: str) -> str:
    """
    Validate that a path is within the current directory and doesn't escape via symlinks.

    Args:
        path: The path to check (relative or absolute)

    Returns:
        The original path if valid

    Raises:
        ValueError: If the path is outside the current directory or symlinks outside
    """
    # Convert the input path to absolute and resolve all symlinks
    # Relative paths are resolved relative to the worktree, not CWD
    try:
        resolved_path = (Path(maca.worktree_path) / path).resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"Cannot resolve path '{path}': {e}")

    # Check if the resolved path is within the current directory
    try:
        resolved_path.relative_to(maca.worktree_path)
    except ValueError:
        raise ValueError(f"Path '{path}' (resolves to '{resolved_path}') is outside the worktree directory '{maca.worktree_path}'")

    return resolved_path


def parse_gitignore_files(exclude_files: Optional[List[str]]) -> List[str]:
    """
    Parse gitignore-style files and return list of exclusion patterns.

    Args:
        exclude_files: List of paths to gitignore-style files to parse

    Returns:
        List of exclusion patterns extracted from all files
    """
    if not exclude_files:
        return []

    worktree = Path(maca.worktree_path)
    patterns = []

    for ignore_file_path in exclude_files:
        ignore_file = worktree / ignore_file_path
        if not ignore_file.exists() or not ignore_file.is_file():
            continue

        try:
            with open(ignore_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip blank lines and comments
                    if not line or line.startswith('#'):
                        continue
                    # Skip negation patterns (! prefix) - would require more complex logic
                    if line.startswith('!'):
                        continue
                    # Remove trailing spaces
                    line = line.rstrip()
                    patterns.append(line)
        except Exception:
            # Skip files that can't be read
            continue

    return patterns


def get_matching_files(
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[List[str]] = None
) -> List[Path]:
    """
    Get list of files matching include/exclude glob patterns.

    Args:
        include: Glob pattern(s) to include. Can be None, a string, or list of strings.
                 Defaults to "**" (all files).
        exclude: Glob pattern(s) to exclude. Can be None, a string, or list of strings.
                 Defaults to ".*" (hidden files/directories).
        exclude_files: List of paths to gitignore-style files to read exclusion patterns from.
                      Defaults to None.

    Returns:
        List of Path objects for matching files (not directories)
    """
    worktree = Path(maca.worktree_path)

    # Normalize include patterns
    if include is None:
        include_patterns = ["**"]
    elif isinstance(include, str):
        include_patterns = [include]
    else:
        include_patterns = include

    # Normalize exclude patterns
    if exclude is None:
        exclude_patterns = []
    elif isinstance(exclude, str):
        exclude_patterns = [exclude]
    else:
        exclude_patterns = exclude

    # Add patterns from exclude_files
    if exclude_files:
        gitignore_patterns = parse_gitignore_files(exclude_files)
        exclude_patterns = list(exclude_patterns) + gitignore_patterns

    # Collect all matching files
    matching_files = set()

    for pattern in include_patterns:
        for path in worktree.glob(pattern):
            if path.is_file():
                matching_files.add(path)

    # Filter out excluded files
    if exclude_patterns:
        filtered_files = []
        for file_path in matching_files:
            rel_path_str = str(file_path.relative_to(worktree))

            # Check if any exclude pattern matches
            excluded = False
            for exc_pattern in exclude_patterns:
                # Check if pattern matches any part of the path
                if fnmatch(rel_path_str, exc_pattern):
                    excluded = True
                    break
                # Also check individual path components
                for part in Path(rel_path_str).parts:
                    if fnmatch(part, exc_pattern):
                        excluded = True
                        break
                if excluded:
                    break

            if not excluded:
                filtered_files.append(file_path)

        return sorted(filtered_files)
    else:
        return sorted(matching_files)


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
            'description': '**Very brief** (max 20 words) explanation of why this tool is being called and what you expect to accomplish'
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


def get_tool_schemas(tool_names: List[str], add_rationale: bool = True) -> List[Dict]:
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

@dataclass
class ReadyResult:
    result: Any

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
        full_path = check_path(file_path)

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
def list_files(
    include: Union[str, List[str]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[List[str]] = None,
    max_files: int = 50
) -> Dict[str, Any]:
    """
    List files in the worktree matching include/exclude glob patterns.

    Returns a random sampling if more files match than max_files.
    Use this to get an impression of what files exist matching a pattern.

    Examples:
    - include="**/*.py" - All Python files
    - include=["**/*.py", "**/*.md"] - Python and Markdown files
    - include="src/**/*.py" - Python files in src/
    - include="**/*test*.py" - Python test files
    - include="*" - Files in top directory only (no subdirectories)
    - exclude=[".*", "**/__pycache__/**"] - Exclude hidden files and pycache

    Args:
        include: Glob pattern(s) to include (default: "**" for all files)
        exclude: Glob pattern(s) to exclude (default: ".*" for hidden files)
        exclude_files: List of paths to gitignore-style files to read exclusion patterns from (default: ['.gitignore'])
        max_files: Maximum number of files to return (default: 50)

    Returns:
        Dict with 'files' (list of file info objects) and optional 'unlisted_files_count' if more files matched than max_files.
        Each file object contains:
        - path: relative path string
        - bytes: file size in bytes
        - lines: number of lines (for text files, omitted for binary/large files)
        - type: "executable" (omitted for regular files)

        If 'unlisted_files_count' > 0, 'files' contains random sampling, which can be
        helpful for getting an impression of a directory structure with many files.
    """
    # Default exclude_files to ['.gitignore'] if not specified
    if exclude_files is None:
        exclude_files = ['.gitignore']
    worktree = Path(maca.worktree_path)

    def get_file_info(path: Path) -> Dict[str, Any]:
        """Get detailed info about a file."""
        rel_path_str = str(path.relative_to(worktree))
        info = {"path": rel_path_str}

        try:
            stat = path.lstat()

            # Get file size
            info["bytes"] = stat.st_size

            # Check if executable
            if stat.st_mode & 0o111:
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

    # Get matching files using helper
    matching_files = get_matching_files(include=include, exclude=exclude, exclude_files=exclude_files)

    # Build file info for each match
    matches = [get_file_info(path) for path in matching_files]

    total_count = len(matches)

    # If we have more matches than max_files, return random sampling
    if total_count > max_files:
        matches = random.sample(matches, max_files)

    # Sort by path
    matches.sort(key=lambda x: x["path"])

    result = {
        'files': matches
    }
    if total_count > max_files:
        result['unlisted_files_count'] = total_count - max_files
    return result


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
        full_path = check_path(update['file_path'])

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
def search(
    regex: str,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[List[str]] = None,
    max_results: int = 10,
    lines_before: int = 2,
    lines_after: int = 2
) -> List[Dict[str, Any]]:
    """
    Search for a regex pattern in file contents, filtering files by glob patterns.

    Args:
        regex: Regular expression to search for in file contents
        include: Glob pattern(s) to include (default: "**" for all files)
        exclude: Glob pattern(s) to exclude (default: ".*" for hidden files)
        exclude_files: List of paths to gitignore-style files to read exclusion patterns from (default: ['.gitignore'])
        max_results: Maximum number of matches to return
        lines_before: Number of context lines before each match
        lines_after: Number of context lines after each match

    Returns:
        List of matches with file_path, line_number, and context lines
    """
    # Default exclude_files to ['.gitignore'] if not specified
    if exclude_files is None:
        exclude_files = ['.gitignore']

    worktree = Path(maca.worktree_path)
    results = []
    content_pattern = re.compile(regex)

    # Get matching files using helper
    matching_files = get_matching_files(include=include, exclude=exclude, exclude_files=exclude_files)

    for file_path in matching_files:
        rel_path_str = str(file_path.relative_to(worktree))

        try:
            with open(file_path, 'r') as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if content_pattern.search(line):
                    start = max(0, i - lines_before)
                    end = min(len(lines), i + lines_after + 1)
                    context = ''.join(lines[start:end])

                    results.append({
                        'file_path': rel_path_str,
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

    if docker_runs is None:
        docker_runs = []

    return run_in_container(
        command=command,
        docker_image=docker_image,
        docker_runs=docker_runs,
        head=head,
        tail=tail
    )


@tool
def subcontext_complete(result: str) -> bool:
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
    return ReadyResult(f"Task completed with result:\n{result}")

@tool
def ask_main_question(question: str) -> bool:
    """
    In case you need clarification from the main context before completing your task,
    you can use this tool to ask a question.
    """
    return ReadyResult(f"The subcontext has a question for the main context:\n{question}\n\nIf you want the subcontext to proceed, answer its question as guidance in a continue_subcontext call. If needed, you can get_user_input first.")

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
            return pt_prompt(f"{prompt}\n> ", history=maca.history)
        return result
    else:
        # Simple text input
        return pt_prompt(f"{prompt}\n> ", history=maca.history)


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
    # Validate context type - reject special (underscore-prefixed) context types
    if context_type.startswith('_'):
        raise ValueError(f"Cannot create subcontext of type '{context_type}'. Types starting with '_' are reserved for special contexts.")

    # Create subcontext (it will auto-generate unique name and register itself)
    subcontext = Context(
        context_type=context_type,
        model=model,
        initial_message=task
    )

    color_print(
        '  ',
        ('ansigreen', 'Created subcontext:'),
        f' {subcontext.context_id} ({context_type}), budget: {budget}μ$',
    )

    # Run the subcontext autonomously
    return subcontext.run(budget=budget)


@tool
def run_oneshot_per_file(
    task: str,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[List[str]] = None,
    file_limit: int = 5,
    model: str = "auto"
) -> str:
    """
    Run a one-shot file_processor on each file matching include/exclude glob patterns.

    Creates one file_processor instance per file. Each instance:
    - Receives the file contents and task
    - Has access to only update_files_and_complete tool
    - Terminates after calling the tool (one-shot execution)

    Useful for applying mechanical changes across multiple files, converting multiple files, or analyzing
    multiple files individually, while keeping context sizes small.

    Args:
        task: Task description for the file_processor (applied to each file)
        include: Glob pattern(s) to include (default: "**" for all files)
        exclude: Glob pattern(s) to exclude (default: ".*" for hidden files)
        exclude_files: List of paths to gitignore-style files to read exclusion patterns from (default: ['.gitignore'])
        file_limit: Maximum files to process (default: 5, prevents accidental bulk operations)
        model: Model to use ("auto" for default, or specific model like "qwen/qwen3-coder-30b-a3b-instruct")

    Returns:
        An object keyed by path names with values being {completed: bool, result: str, cost: number} objects.
    """
    # Default exclude_files to ['.gitignore'] if not specified
    if exclude_files is None:
        exclude_files = ['.gitignore']

    # Get list of files matching the patterns
    file_list_result = list_files(include=include, exclude=exclude, exclude_files=exclude_files, max_files=file_limit)
    files = file_list_result['files']

    if 'unlisted_files_count' in file_list_result:
        return f"Error: Too many files match the pattern. Limit is {file_limit}. Please narrow the pattern or increase limit."

    if not files:
        return f"No files found matching patterns"

    # Process each file
    results = {}

    for i, file_info in enumerate(files, 1):
        file_path = file_info['path']

        color_print('  ', ('ansicyan', f'[{i}/{len(files)}] Processing: {file_path}'))

        # Read file contents
        file_result = read_files([file_path])
        if file_result[0].get('error'):
            error_msg = f"Error reading {file_path}: {file_result[0]['error']}"
            results.append(error_msg)
            continue

        file_contents = file_result[0]['data']

        # Create file_processor context (special type, doesn't auto-register)
        initial_message = f"{task}\n\nFile: {file_path}\n\nContents:\n\n{file_contents}"
        context = Context(context_type='_file_processor', model=model, initial_message=initial_message)

        # Run context (will execute once and complete)
        run_result = context.run()

        # Collect result
        results[file_path] = run_result

    return results


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

    if unique_name not in maca.subcontexts:
        raise ValueError(f"Unknown subcontext: {unique_name}")

    subcontext = maca.subcontexts[unique_name]

    # Add guidance if provided
    if guidance:
        subcontext.add_message({"role": "user", "content": guidance})

    color_print(
        '  ',
        ('ansigreen', 'Continuing subcontext:'),
        f' {unique_name}, budget: {budget}μ$',
    )

    # Run the subcontext autonomously
    return subcontext.run(budget=budget)


@tool
def main_complete(result: str, commit_msg: str | None) -> bool:
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
        result: Answer to the user's question, or a short summary of what was accomplished
        commit_msg: Optional git commit message summarizing all the changes made (if any).
           This should address only the final result, not intermediate steps. If no changes
           were made, this should be `null`.
    """

    color_print('\n', ('ansigreen', 'Task completed!'), f'\n{result}\n')

    print(result)

    # Ask for approval
    response = radiolist_dialog(
        title='Task Complete',
        text='Are you satisfied with the result?',
        values=[
            ('yes', 'Yes, merge into main'),
            ('no', 'No, I want changes'),
            ('cancel', 'Cancel (keep worktree for manual review)')
        ]
    ).run()

    if response == 'yes':
        color_print(('ansicyan', 'Merging changes...'))

        # Merge
        success, message = git_ops.merge_to_main(maca.repo_root, maca.worktree_path, maca.branch_name, commit_msg or result)

        if success:
            # Cleanup
            git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
            color_print(('ansigreen', '✓ Merged and cleaned up'))
        else:
            color_print(('ansired', f'Merge failed: {message}'))
            print("You may need to resolve conflicts manually or spawn a merge context.")

        return ReadyResult(result)
    
    elif response == 'no':
        feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=maca.history)
        maca.main_context.add_message({"role": "user", "content": feedback})
        return 'User rejected result and provided feedback.'
    else:
        print("Keeping worktree for manual review.")
        return ReadyResult(result)


@tool
def update_files_and_complete(updates: List[Dict[str, str]], result: str) -> bool:
    """
    Update files and signal completion. This is the ONLY tool available to file_processor contexts.

    This is a one-shot tool - the context terminates immediately after calling it.

    Each update can either:
        1. Write entire file: {"file_path": "path/to/file", "data": "new content"}
        2. Search and replace: {"file_path": "path/to/file", "old_data": "search", "new_data": "replacement", "allow_multiple": true}

    Note that for search and replace, old_data must match exactly (including whitespace and newlines). If it does not appear or appears
    multiple times (and allow_multiple is false), an error is raised. "allow_multiple" default to false (so you can leave it out for
    the common case of single replacement).

    Args:
        updates: List of file update specifications
        result: Brief summary of what was done with this file (goes to Main context)
    """
    # First perform the updates
    if updates:
        for update in updates:
            file_path = update['file_path']
            full_path = Path(maca.worktree_path) / file_path

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

    return ReadyResult(result)
