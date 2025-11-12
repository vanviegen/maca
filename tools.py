#!/usr/bin/env python3
"""Tool system with reflection-based schema generation."""

from dataclasses import dataclass
import inspect
import re
import json
import random
from fnmatch import fnmatch
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from pathlib import Path
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union, Optional

from utils import cprint, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO
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
        The resolved absolute path if valid

    Raises:
        ValueError: If the path is outside the current directory or symlinks outside
    """
    # Convert the input path to absolute and resolve all symlinks
    # Relative paths are resolved relative to the worktree, not CWD
    try:
        resolved_path = (Path(worktree_path) / path).resolve()
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
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[Union[str, List[str]]] = None
) -> List[Path]:
    """
    Get list of files matching include/exclude glob patterns.

    Args:
        worktree_path: Path to the worktree
        include: Glob pattern(s) to include. Can be None, a string, or list of strings.
                 Defaults to "**" (all files).
        exclude: Glob pattern(s) to exclude. Can be None, a string, or list of strings.
                 Defaults to ".*" (hidden files/directories).
        exclude_files: File(s) containing exclude patterns (e.g., ".gitignore"). Can be None, a string, or list of strings.
                       Defaults to None. When ".gitignore" is included, gitignore semantics are applied.

    Returns:
        List of Path objects for matching files (not directories)
    """
    from utils import parse_gitignore
    
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

    # Normalize exclude_files
    if exclude_files is None:
        exclude_file_list = []
    elif isinstance(exclude_files, str):
        exclude_file_list = [exclude_files]
    else:
        exclude_file_list = exclude_files

    # Parse .gitignore if present in exclude_files
    gitignore_matcher = None
    if '.gitignore' in exclude_file_list:
        gitignore_path = worktree / '.gitignore'
        gitignore_matcher = parse_gitignore(gitignore_path)

    # Collect all matching files
    matching_files = set()

    for pattern in include_patterns:
        for path in worktree.glob(pattern):
            if path.is_file():
                matching_files.add(path)

    # Filter out excluded files
    filtered_files = []
    for file_path in matching_files:
        rel_path_str = str(file_path.relative_to(worktree))

        # Check gitignore first
        if gitignore_matcher:
            if gitignore_matcher.matches(rel_path_str, is_dir=False):
                continue

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
def update_files(
    updates: List[Dict[str, str]],
    summary: Optional[str] = None,
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[str, str]:
    """
    Update one or more files with new content.

    Each update can either:
    1. Write entire file: {"file_path": "path/to/file", "data": "new content"}
    2. Search and replace: {"file_path": "path/to/file", "old_data": "search", "new_data": "replacement", "allow_multiple": false}

    Args:
        updates: List of update specifications
        summary: Optional custom summary for long-term context. If not provided, a default summary is generated.

    Returns:
        Immediate: "OK"
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

    return ("OK", final_summary)


@tool
def search(
    regex: str,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[Union[str, List[str]]] = None,
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
        exclude_files: File(s) containing exclude patterns (e.g., ".gitignore"). Defaults to None.
        max_results: Maximum number of matches to return
        lines_before: Number of context lines before each match
        lines_after: Number of context lines after each match

    Returns:
        Immediate: List of matches with file_path, line_number, and context lines
        Long-term context: Brief summary (e.g., "search: found 5 matches in 3 files")
    """
    # Default exclude_files to ['.gitignore'] if not specified
    if exclude_files is None:
        exclude_files = ['.gitignore']
    
    worktree = Path(worktree_path)
    results = []
    content_pattern = re.compile(regex)
    files_with_matches = set()

    # Get matching files using helper
    matching_files = get_matching_files(
        worktree_path=worktree_path, 
        include=include, 
        exclude=exclude,
        exclude_files=exclude_files
    )

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
    Execute a shell command in a Docker container. The cwd will be the worktree path.

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



@dataclass
class Question:
    """A single question with optional preset answers."""
    prompt: str
    preset_answers: Optional[List[str]] = None


@tool
def ask_user_questions(
    questions: List[Dict[str, Any]],
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> tuple[str, str]:
    """
    Ask the user one or more questions interactively.

    Each question can optionally provide preset answer choices. The LLM is highly encouraged
    to provide likely answers when possible to make it easier for the user.

    Args:
        questions: List of question objects, each with:
            - prompt: The question to ask the user
            - preset_answers: Optional list of likely answer choices

    Returns:
        Immediate: A formatted string containing all answers, clearly separated
        Long-term context: Same as immediate (answers are typically short)
    """
    answers = []
    
    for i, q in enumerate(questions, 1):
        prompt_text = q.get('prompt', '')
        preset_answers = q.get('preset_answers')
        
        if preset_answers:
            # Show choice selection
            choices = [(answer, answer) for answer in preset_answers]
            choices.append(('__custom__', 'Other (custom input)'))

            result = choice(
                message=f"Question {i}/{len(questions)}: {prompt_text}",
                options=choices
            )

            if result == '__custom__':
                answer = pt_prompt(f"> ", history=history)
            else:
                answer = result
        else:
            # Simple text input
            answer = pt_prompt(f"Question {i}/{len(questions)}: {prompt_text}\n> ", history=history)
        
        answers.append(f"Q{i}: {prompt_text}\nA{i}: {answer}")
    
    result = "\n\n".join(answers)
    return (result, result)




@tool
def process_files(
    instructions: str,
    batches: List[List[Dict[str, Any]]],
    model: str = "anthropic/claude-sonnet-4.5",
    worktree_path: Path = None,
    repo_root: Path = None,
    history = None,
    maca = None
) -> Dict[str, Any]:
    """
    Read and process files with instructions using separate LLM calls.

    Each batch is processed with its own LLM call that has access to all tools. This ensures
    file contents never remain in the main context - they're shown once with ephemeral cache,
    then replaced with a summary.

    **When to use single vs multiple batches:**
    - **Single batch**: Use when files need coordinated changes or analysis together.
      One LLM call processes all files and can make coordinated tool calls.
    - **Multiple batches**: Use when making mechanical/repetitive changes to many files where each 
      file (or small group) can be processed independently. Each batch gets its own LLM call.

    **Batch structure:**
    Each batch is a list of file specifications:
    ```
    {
        "path": "relative/path/to/file.py",
        "start_line": 10,  # Optional: first line to read (1-indexed)
        "end_line": 50     # Optional: last line to read (inclusive)
    }
    ```

    **Examples:**
    ```python
    # Single batch - process 3 related files together
    batches=[
        [
            {"path": "main.py"},
            {"path": "utils.py"},
            {"path": "config.py"}
        ]
    ]

    # Multiple batches - process each file independently
    batches=[
        [{"path": "file1.py"}],
        [{"path": "file2.py"}],
        [{"path": "file3.py"}]
    ]

    # Batch with line ranges
    batches=[
        [
            {"path": "large_file.py", "start_line": 1, "end_line": 100},
            {"path": "large_file.py", "start_line": 101, "end_line": 200}
        ]
    ]
    ```

    Args:
        instructions: Instructions for processing the file(s)
        batches: List of batches, where each batch is a list of file specs
        model: Model to use for batch processing (default: anthropic/claude-sonnet-4.5)

    Returns:
        Immediate: Dict keyed by batch index with {success: bool, result: str, cost: int, tool_called: str}
        Long-term context: Summary like "process_files: processed 5 batches (4 successful)"
    """
    if not batches:
        error_result = {"error": "No batches specified"}
        return (error_result, "process_files: error")

    # All batches are processed with separate LLM calls
    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        return ({"error": "OPENROUTER_API_KEY not set"}, "process_files: error")

    # Get all tool schemas
    tool_schemas = get_all_tool_schemas(add_rationale=False)

    # Process each batch
    results = {}

    for batch_idx, batch in enumerate(batches):
        cprint(C_INFO, f'  [{batch_idx + 1}/{len(batches)}] Processing batch of {len(batch)} files')

        # Read files in this batch
        batch_contents = []
        for file_spec in batch:
            file_path = file_spec['path']
            start_line = file_spec.get('start_line')
            end_line = file_spec.get('end_line')

            full_path = check_path(file_path, worktree_path)
            
            if not full_path.exists():
                batch_contents.append(f"File: {file_path}\n\nError: File not found")
                continue

            try:
                with open(full_path, 'r') as f:
                    lines = f.readlines()

                # Handle line range
                if start_line is not None or end_line is not None:
                    start_idx = (start_line - 1) if start_line else 0
                    end_idx = end_line if end_line else len(lines)
                    selected_lines = lines[start_idx:end_idx]
                    data = ''.join(selected_lines)
                    batch_contents.append(
                        f"File: {file_path} (lines {start_idx + 1}-{end_idx})\n\n{data}"
                    )
                else:
                    data = ''.join(lines)
                    batch_contents.append(f"File: {file_path}\n\n{data}")
            except Exception as e:
                batch_contents.append(f"File: {file_path}\n\nError: {str(e)}")

        # Build messages for this batch
        batch_content = "\n\n---\n\n".join(batch_contents)
        messages = [
            {'role': 'system', 'content': instructions},
            {'role': 'user', 'content': batch_content}
        ]

        # Call LLM using shared utility
        from utils import call_llm
        
        try:
            llm_result = call_llm(
                api_key=api_key,
                model=model,
                messages=messages,
                tool_schemas=tool_schemas,
                logger=None  # No logger for process_files LLM calls
            )
            
            message = llm_result['message']
            cost = llm_result['cost']

            # Extract and execute tool call
            tool_calls = message.get('tool_calls', [])
            if not tool_calls:
                results[batch_idx] = {
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

            results[batch_idx] = {
                'success': True,
                'result': str(tool_result),
                'cost': cost,
                'tool_called': called_tool_name
            }

        except Exception as e:
            results[batch_idx] = {
                'success': False,
                'result': f'Error: {str(e)}',
                'cost': 0
            }

    # Build summary
    success_count = sum(1 for r in results.values() if r['success'])
    total_count = len(results)
    summary = f"process_files: processed {total_count} batches ({success_count} successful)"

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

    cprint(C_GOOD, '\n✓ Task completed!\n\n', C_NORMAL, result, '\n')

    print(result)

    # Ask for approval
    response = choice(
        message=f'{worktree_path} -- How to proceed?',
        options=[
            ('merge', 'Merge into main'),
            ('continue', 'Ask for further changes'),
            ('cancel', 'Leave as-is for manual review'),
            ('delete', 'Delete everything'),
        ]
    )

    if response == 'merge':
        cprint(C_INFO, 'Merging changes...')

        # Merge
        conflict = git_ops.merge_to_main(repo_root, worktree_path, maca.branch_name, commit_msg or result)

        if conflict:
            cprint(C_BAD, "⚠ Merge conflicts!")
            return f"Merge conflict while rebasing. Please resolve merge conflicts by reading the affected files and using update_files to resolve the conflicts. Then use shell tool with `git add <filename>.. && git rebase --continue`, before calling complete again with the same arguments to try the merge again. Here is the rebase output:\n\n{conflict}"
        
        # Cleanup
        git_ops.cleanup_session(repo_root, worktree_path, maca.branch_name)
        cprint(C_GOOD, '✓ Merged and cleaned up')

        return ReadyResult(result)

    elif response == 'continue':
        feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=history)
        maca.add_message({"role": "user", "content": feedback})
        return 'User rejected result and provided feedback.'
    
    elif response == 'delete':
        git_ops.cleanup_session(repo_root, worktree_path, maca.branch_name)
        cprint(C_BAD, '✓ Deleted worktree and branch')
        return ReadyResult(result)
    
    else:  # cancel
        print("Keeping worktree for manual review.")
        return ReadyResult(result)


