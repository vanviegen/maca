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

from utils import color_print
from docker_ops import run_in_container
import git_ops
import json
import time
import urllib.request
import os


def check_path(path: str, worktree_path: Path) -> str:
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
    try:
        resolved_path = Path(path).resolve()
    except (OSError, RuntimeError) as e:
        raise ValueError(f"Cannot resolve path '{path}': {e}")
    
    # Check if the resolved path is within the current directory
    try:
        resolved_path.relative_to(worktree_path)
    except ValueError:
        raise ValueError(f"Path '{path}' (resolves to '{resolved_path}') is outside the worktree directory '{worktree_path}'")

    return resolved_path


def get_matching_files(
    worktree_path: Path,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*"
) -> List[Path]:
    """
    Get list of files matching include/exclude glob patterns.

    Args:
        worktree_path: Path to the worktree
        include: Glob pattern(s) to include. Can be None, a string, or list of strings.
                 Defaults to "**" (all files).
        exclude: Glob pattern(s) to exclude. Can be None, a string, or list of strings.
                 Defaults to ".*" (hidden files/directories).

    Returns:
        List of Path objects for matching files (not directories)
    """
    worktree = Path(worktree_path)

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


def get_all_tool_schemas(add_rationale: bool = False) -> List[Dict]:
    """
    Get schemas for all registered tools.

    Args:
        add_rationale: Whether to add rationale parameter to all tools

    Returns:
        List of all tool schemas
    """
    return get_tool_schemas(list(_TOOLS.keys()), add_rationale=add_rationale)


def execute_tool(tool_name: str, arguments: Dict, worktree_path: Path, repo_root: Path, history, maca) -> Any:
    """
    Execute a tool with the given arguments.

    Args:
        tool_name: Name of the tool to execute
        arguments: Tool arguments
        worktree_path: Path to the worktree
        repo_root: Path to the repository root
        history: Prompt history for user input
        maca: MACA instance for accessing context

    Returns:
        Tuple of (immediate_result, context_summary) where:
        - immediate_result: Full data for next LLM call
        - context_summary: Brief summary for long-term context
    """
    if tool_name not in _TOOLS:
        raise ValueError(f"Unknown tool: {tool_name}")

    tool_info = _TOOLS[tool_name]
    func = tool_info['function']

    # Remove rationale from arguments before calling (it's just for logging)
    exec_args = {k: v for k, v in arguments.items() if k != 'rationale'}

    # Add context parameters
    exec_args['worktree_path'] = worktree_path
    exec_args['repo_root'] = repo_root
    exec_args['history'] = history
    exec_args['maca'] = maca

    result = func(**exec_args)

    # Tools should return tuples, but handle both cases for compatibility
    if isinstance(result, tuple) and len(result) == 2:
        return result
    else:
        # Legacy tool that doesn't return a tuple - return as-is with generic summary
        return (result, f"{tool_name}: executed")

@dataclass
class ReadyResult:
    result: Any

# ==============================================================================
# TOOLS
# ==============================================================================

def _read_files_helper(file_paths: List[str], worktree_path: Path, max_lines: int = None) -> List[Dict[str, Any]]:
    """
    Helper function to read files. Not exposed as a tool.

    Args:
        file_paths: List of file paths to read
        worktree_path: Path to the worktree
        max_lines: Maximum number of lines to read per file (None = all lines)

    Returns:
        List of dicts with file_path, data, and metadata for each file
    """
    results = []

    for file_path in file_paths:
        full_path = check_path(file_path, worktree_path)

        if not full_path.exists():
            results.append({
                'file_path': file_path,
                'error': 'File not found',
                'data': ''
            })
            continue

        try:
            with open(full_path, 'r') as f:
                lines = f.readlines()

            total_lines = len(lines)

            if max_lines is not None:
                selected_lines = lines[:max_lines]
                data = ''.join(selected_lines)
                remaining = max(0, total_lines - max_lines)
            else:
                data = ''.join(lines)
                remaining = 0

            results.append({
                'file_path': file_path,
                'data': data,
                'total_lines': total_lines,
                'remaining_lines': remaining
            })
        except Exception as e:
            results.append({
                'file_path': file_path,
                'error': str(e),
                'data': ''
            })

    return results


@tool
def list_files(
    include: Union[str, List[str]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    max_files: int = 50,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[Dict[str, Any], str]:
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
        max_files: Maximum number of files to return (default: 50)

    Returns:
        Immediate: Dict with 'total_count' and 'files' list
        Long-term context: Brief summary (e.g., "list_files: found 15 files")
    """
    worktree = Path(worktree_path)

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
    matching_files = get_matching_files(worktree_path=worktree_path, include=include, exclude=exclude)

    # Build file info for each match
    matches = [get_file_info(path) for path in matching_files]

    total_count = len(matches)

    # If we have more matches than max_files, return random sampling
    if total_count > max_files:
        matches = random.sample(matches, max_files)

    # Sort by path
    matches.sort(key=lambda x: x["path"])

    result = {
        'total_count': total_count,
        'files': matches
    }

    # Build summary
    pattern_str = include if isinstance(include, str) else f"{len(include)} patterns"
    if total_count > max_files:
        summary = f"list_files: found {total_count} files (showing {max_files} random sample)"
    else:
        summary = f"list_files: found {total_count} files matching {pattern_str}"

    return (result, summary)


@tool
def update_files(
    updates: List[Dict[str, str]],
    summary: Optional[str] = None,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[None, str]:
    """
    Update one or more files with new content.

    Each update can either:
    1. Write entire file: {"file_path": "path/to/file", "data": "new content"}
    2. Search and replace: {"file_path": "path/to/file", "old_data": "search", "new_data": "replacement", "allow_multiple": false}

    Args:
        updates: List of update specifications
        summary: Optional custom summary for long-term context. If not provided, a default summary is generated.

    Returns:
        Immediate: None
        Long-term context: Custom summary or default listing files modified/created
    """
    modified_files = []
    created_files = []

    for update in updates:
        file_path = update['file_path']
        full_path = check_path(file_path, worktree_path)

        # Track if file exists before update
        existed_before = full_path.exists()

        # Ensure parent directory exists
        full_path.parent.mkdir(parents=True, exist_ok=True)

        if 'data' in update:
            # Full file write
            full_path.write_text(update['data'])
            if existed_before:
                modified_files.append(file_path)
            else:
                created_files.append(file_path)
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
            modified_files.append(file_path)
        else:
            raise ValueError(f"Invalid update specification: {update}")

    # Use custom summary if provided, otherwise generate default
    if summary:
        final_summary = f"update_files: {summary}"
    else:
        summary_parts = []
        if modified_files:
            summary_parts.append(f"modified {', '.join(modified_files)}")
        if created_files:
            summary_parts.append(f"created {', '.join(created_files)}")
        final_summary = "update_files: " + "; ".join(summary_parts) if summary_parts else "update_files: no changes"

    return (None, final_summary)


@tool
def search(
    regex: str,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    max_results: int = 10,
    lines_before: int = 2,
    lines_after: int = 2,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[List[Dict[str, Any]], str]:
    """
    Search for a regex pattern in file contents, filtering files by glob patterns.

    Args:
        regex: Regular expression to search for in file contents
        include: Glob pattern(s) to include (default: "**" for all files)
        exclude: Glob pattern(s) to exclude (default: ".*" for hidden files)
        max_results: Maximum number of matches to return
        lines_before: Number of context lines before each match
        lines_after: Number of context lines after each match

    Returns:
        Immediate: List of matches with file_path, line_number, and context lines
        Long-term context: Brief summary (e.g., "search: found 5 matches in 3 files")
    """
    worktree = Path(worktree_path)
    results = []
    content_pattern = re.compile(regex)
    files_with_matches = set()

    # Get matching files using helper
    matching_files = get_matching_files(worktree_path=worktree_path, include=include, exclude=exclude)

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
                    files_with_matches.add(rel_path_str)

                    if len(results) >= max_results:
                        summary = f"search: found {len(results)}+ matches in {len(files_with_matches)}+ files (limit reached)"
                        return (results, summary)
        except Exception:
            # Skip files that can't be read
            continue

    summary = f"search: found {len(results)} matches in {len(files_with_matches)} files"
    return (results, summary)


@tool
def shell(
    command: str,
    docker_image: str = "debian:stable",
    docker_runs: List[str] = None,
    head: int = 50,
    tail: int = 50,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[Dict[str, Any], str]:
    """
    Execute a shell command in a Docker container.

    Args:
        command: Shell command to execute
        docker_image: Base Docker image to use
        docker_runs: List of RUN commands to execute when building the image (e.g., ["RUN apt-get update && apt-get install -y nodejs"])
        head: Number of lines to keep from start of output
        tail: Number of lines to keep from end of output

    Returns:
        Immediate: Dict with stdout, stderr, and exit_code
        Long-term context: Command string (truncated to 128 chars)
    """

    if docker_runs is None:
        docker_runs = []

    result = run_in_container(
        command=command,
        worktree_path=worktree_path,
        repo_root=repo_root,
        docker_image=docker_image,
        docker_runs=docker_runs,
        head=head,
        tail=tail
    )

    # Truncate command for summary
    cmd_summary = command[:128]
    if len(command) > 128:
        cmd_summary += "..."

    summary = f"shell: {cmd_summary}"

    return (result, summary)



@tool
def get_user_input(
    prompt: str,
    preset_answers: List[str] = None,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[str, str]:
    """
    Get input from the user interactively.

    Args:
        prompt: The prompt to display to the user
        preset_answers: Optional list of preset answer choices

    Returns:
        Immediate: The user's input
        Long-term context: "get_user_input: {answer}"
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
            answer = pt_prompt(f"{prompt}\n> ", history=history)
        else:
            answer = result
    else:
        # Simple text input
        answer = pt_prompt(f"{prompt}\n> ", history=history)

    # Build summary - truncate long answers
    answer_summary = answer[:100]
    if len(answer) > 100:
        answer_summary += "..."
    summary = f"get_user_input: {answer_summary}"

    return (answer, summary)




@tool
def process_files(
    instructions: str,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    file_limit: int = 5,
    single_batch: bool = True,
    model: str = "anthropic/claude-sonnet-4.5",
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> Dict[str, Any]:
    """
    Read and process files with instructions.

    **Single Batch Mode** (single_batch=True, default):
    - Loads all matching files into context at once
    - Returns file contents to you in the immediate response
    - You then make tool calls (typically update_files or complete) in the main loop
    - Long file contents are automatically replaced with summary after you've seen them

    **Per-File Mode** (single_batch=False):
    - Each file is processed individually with separate LLM calls
    - Good for mechanical changes where files are independent

    Args:
        instructions: Instructions for processing the file(s)
        include: Glob pattern(s) to include (default: "**" for all files)
        exclude: Glob pattern(s) to exclude (default: ".*" for hidden files)
        file_limit: Maximum files to process (default: 5, prevents accidental bulk operations)
        single_batch: If True, load all files at once; if False, process each file separately
        model: Model to use for per-file mode (default: anthropic/claude-sonnet-4.5)

    Returns:
        Single batch mode:
            Immediate: All file contents formatted as "File: path\n\ncontents\n\n---\n\n"
            Long-term context: Summary like "process_files: loaded 3 files"

        Per-file mode:
            Immediate: Dict keyed by file path with {success: bool, result: str, cost: int}
            Long-term context: Summary like "process_files: processed 5 files (4 successful)"
    """
    # Get list of files matching the patterns
    file_list_result, _ = list_files(include=include, exclude=exclude, max_files=file_limit, worktree_path=worktree_path, repo_root=repo_root, history=history, maca=maca)
    files = file_list_result['files']

    error_result = None
    if len(files) < file_list_result['total_count']:
        error_result = {"error": f"Too many files match the pattern. Limit is {file_limit}. Please narrow the pattern or increase limit."}
    elif not files:
        error_result = {"error": "No files found matching patterns"}

    if error_result:
        return (error_result, f"process_files: error")

    if single_batch:
        # Single batch mode: load all files and return contents
        color_print('  ', ('ansicyan', f'Loading {len(files)} files in batch mode...'))

        # Read all files
        file_contents_list = []
        for file_info in files:
            file_path = file_info['path']
            file_result = _read_files_helper([file_path], worktree_path)
            if file_result[0].get('error'):
                file_contents_list.append(f"File: {file_path}\n\nError: {file_result[0]['error']}")
            else:
                file_contents_list.append(f"File: {file_path}\n\n{file_result[0]['data']}")

        # Build the immediate result with instructions and all file contents
        immediate_result = f"{instructions}\n\n{'='*60}\n\n" + "\n\n---\n\n".join(file_contents_list)
        summary = f"process_files: loaded {len(files)} files"

        color_print('  ', ('ansigreen', f'✓ Loaded {len(files)} files'))

        return (immediate_result, summary)

    else:
        # Per-file mode: process each file individually
        api_key = os.environ.get('OPENROUTER_API_KEY')
        if not api_key:
            return ({"error": "OPENROUTER_API_KEY not set"}, "process_files: error")

        # Get all tool schemas (not limited to single tool)
        tool_schemas = get_all_tool_schemas(add_rationale=False)

        # Process each file
        results = {}

        for i, file_info in enumerate(files, 1):
            file_path = file_info['path']

            color_print('  ', ('ansicyan', f'[{i}/{len(files)}] Processing: {file_path}'))

            # Read file contents
            file_result = _read_files_helper([file_path], worktree_path)
            if file_result[0].get('error'):
                results[file_path] = {
                    'success': False,
                    'result': f"Error reading file: {file_result[0]['error']}",
                    'cost': 0
                }
                continue

            file_contents = file_result[0]['data']

            # Build messages
            messages = [
                {'role': 'system', 'content': instructions},
                {'role': 'user', 'content': f"File: {file_path}\n\n{file_contents}"}
            ]

            # Call LLM
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
                'HTTP-Referer': 'https://github.com/vanviegen/maca',
                'X-Title': 'MACA - Coding Assistant'
            }

            data = {
                'model': model,
                'messages': messages,
                'tools': tool_schemas,
                'usage': {"include": True},
                'tool_choice': 'required',
            }

            try:
                req = urllib.request.Request(
                    "https://openrouter.ai/api/v1/chat/completions",
                    data=json.dumps(data).encode('utf-8'),
                    headers=headers
                )
                with urllib.request.urlopen(req) as response:
                    result = json.loads(response.read().decode('utf-8'))

                # Extract response
                choice = result['choices'][0]
                message = choice['message']
                usage = result.get('usage', {})
                cost = int(usage.get('cost', 0) * 1_000_000)  # Convert to microdollars

                # Extract and execute tool call
                tool_calls = message.get('tool_calls', [])
                if not tool_calls:
                    results[file_path] = {
                        'success': False,
                        'result': 'No tool call made by LLM',
                        'cost': cost
                    }
                    continue

                tool_call = tool_calls[0]
                called_tool_name = tool_call['function']['name']
                tool_args = json.loads(tool_call['function']['arguments'])

                # Execute the tool
                tool_result, _ = execute_tool(called_tool_name, tool_args, worktree_path, repo_root, history, maca)

                results[file_path] = {
                    'success': True,
                    'result': str(tool_result),
                    'cost': cost,
                    'tool_called': called_tool_name
                }

            except Exception as e:
                results[file_path] = {
                    'success': False,
                    'result': f'Error: {str(e)}',
                    'cost': 0
                }

        # Build summary
        success_count = sum(1 for r in results.values() if r['success'])
        total_count = len(results)
        summary = f"process_files: processed {total_count} files ({success_count} successful)"

        return (results, summary)




@tool
def complete(
    result: str,
    commit_msg: str | None,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> bool:
    """
    Signal that the user's task is complete and ready for review.

    Only call this when:
    - All work is complete
    - The user's request has been fully satisfied
    - No further work is needed

    Args:
        result: Answer to the user's question, or a short summary of what was accomplished
        commit_msg: Optional git commit message summarizing all the changes made (if any).
           If no changes were made, this should be `null`.
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
        success, message = git_ops.merge_to_main(repo_root, worktree_path, maca.branch_name, commit_msg or result)

        if success:
            # Cleanup
            git_ops.cleanup_session(repo_root, worktree_path, maca.branch_name)
            color_print(('ansigreen', '✓ Merged and cleaned up'))
        else:
            color_print(('ansired', f'Merge failed: {message}'))
            print("You may need to resolve conflicts manually.")

        return ReadyResult(result)

    elif response == 'no':
        feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=history)
        maca.add_message({"role": "user", "content": feedback})
        return 'User rejected result and provided feedback.'
    else:
        print("Keeping worktree for manual review.")
        return ReadyResult(result)


