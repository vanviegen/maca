#!/usr/bin/env python3
"""Tool system with reflection-based schema generation."""

from dataclasses import dataclass
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union, Optional, TypedDict
import inspect
import json
import json
import re

from utils import cprint, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO, get_matching_files, call_llm
from docker_ops import run_in_container        
import git_ops


# Model size mappings for process_files
MODELS = {
    'tiny': 'qwen/qwen3-coder-30b-a3b-instruct',
    'small': 'moonshotai/kimi-linear-48b-a3b-instruct',
    'medium': 'x-ai/grok-code-fast-1',
    'large': 'anthropic/claude-sonnet-4.5',
    'huge': 'anthropic/claude-opus-4.1'
}


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


# Tool registry - single registry for all tools
TOOL_SCHEMAS = {}

def tool(func):
    """
    Decorator to register a function as an LLM tool.

    Tools are registered by name and schemas are generated on-demand
    based on the context that uses them.
    """
    TOOL_SCHEMAS[func.__name__] = generate_tool_schema(func)


def python_type_to_json_type(py_type) -> Dict:
    """Convert Python type hint to JSON schema type."""
    origin = get_origin(py_type)

    # Handle TypedDict
    if hasattr(py_type, '__annotations__'):
        properties = {}
        required_keys = []
        
        for field_name, field_type in get_type_hints(py_type).items():
            properties[field_name] = python_type_to_json_type(field_type)
            
            # Field is required if it's not wrapped in Optional
            field_origin = get_origin(field_type)
            if field_origin is Union:
                # Check if None is in the union args
                if type(None) not in get_args(field_type):
                    required_keys.append(field_name)
            else:
                # Not a Union, so it's required
                required_keys.append(field_name)
        
        schema = {
            'type': 'object',
            'properties': properties,
            'additionalProperties': False
        }
        if required_keys:
            schema['required'] = required_keys
        return schema

    # Handle Union types
    if origin is Union:
        args = get_args(py_type)
        has_none = type(None) in args
        non_none_args = [arg for arg in args if arg is not type(None)]
        
        # Convert each union member to a schema
        schemas = []
        for arg in non_none_args:
            schemas.append(python_type_to_json_type(arg))
        
        if has_none:
            schemas.append({'type': 'null'})
        
        # If only one schema (plus maybe null), simplify
        if len(schemas) == 1:
            return schemas[0]
        
        # Multiple schemas - use anyOf
        return {'anyOf': schemas}

    # Handle List
    if origin is list:
        args = get_args(py_type)
        if args:
            item_schema = python_type_to_json_type(args[0])
            return {'type': 'array', 'items': item_schema}
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
    elif py_type == type(None):
        return {'type': 'null'}
    elif py_type == Any:
        return {}
    else:
        # Default to string for unknown types
        return {'type': 'string'}


def generate_tool_schema(func) -> Dict:
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
        # Skip internal parameters
        if param_name in ('self', 'rationale', 'maca'):
            continue

        param_schema = python_type_to_json_type(type_hints.get(param_name, str))

        # Add description if available
        if param_name in param_docs:
            param_schema['description'] = param_docs[param_name]

        properties[param_name] = param_schema

        # Check if required (no default value)
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    # Add automatic rationale parameter
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


def execute_tool(tool_name: str, arguments: Dict, maca) -> Any:
    """
    Execute a tool with the given arguments.

    Args:
        tool_name: Name of the tool to execute
        arguments: Tool arguments
        maca: MACA instance for accessing context

    Returns:
        Tuple of (immediate_result, context_summary) where:
        - immediate_result: Full data for next LLM call
        - context_summary: Brief summary for long-term context
    """
    if tool_name not in TOOL_SCHEMAS:
        raise ValueError(f"Unknown tool: {tool_name}")

    tool_info = TOOL_SCHEMAS[tool_name]
    func = tool_info['function']

    # Remove rationale from arguments before calling (it's just for logging)
    exec_args = {k: v for k, v in arguments.items() if k != 'rationale'}

    # Add maca instance
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

# Type definitions for update_files
class SearchReplaceOp(TypedDict, total=False):
    """A single search/replace operation."""
    search: str
    replace: str
    min_match: Optional[int]  # Optional, defaults to 1
    max_match: Optional[int]  # Optional, defaults to 1


class FileUpdate(TypedDict, total=False):
    """Specification for updating a single file."""
    path: str
    overwrite: Optional[str]  # Overwrite file with this content
    update: Optional[List[SearchReplaceOp]]  # Apply search/replace operations
    rename: Optional[str]  # Rename to this path (empty string means delete)
    summary: str  # One-sentence description of changes



@tool
def update_files(
    updates: List[FileUpdate],
    maca = None
) -> tuple[str, str]:
    """
    Update, create, or delete one or more files.

    Each update specifies a file path and optional operations to perform.
    Operations are executed in order: overwrite, update, rename.
    Multiple operations can be specified for a single file.

    **Overwrite file (create or replace):**
    ```python
    {"path": "path/to/file", "overwrite": "new content", "summary": "Created config file"}
    ```

    **Delete file:**
    ```python
    {"path": "path/to/file", "rename": "", "summary": "Removed deprecated module"}
    ```

    **Rename/move file:**
    ```python
    {"path": "old/path", "rename": "new/path", "summary": "Moved file to new location"}
    ```

    **Search and replace operations:**
    ```python
    {
        "path": "path/to/file",
        "update": [
            {
                "search": "old text",
                "replace": "new text",
                "min_match": 1,  # Optional, default 1
                "max_match": 1   # Optional, default 1
            }
        ],
        "summary": "Updated function names to new convention"
    }
    ```

    **Multiple operations:**
    ```python
    {
        "path": "path/to/file",
        "update": [{"search": "initial", "replace": "final"}],
        "rename": "new/path",
        "summary": "Created, updated, and moved file"
    }
    ```

    **No operations (logging only):**
    ```python
    {"path": "path/to/file", "summary": "No legacy code found"}
    ```

    For search/replace operations:
    - `min_match` and `max_match` default to 1 (exactly one match required)
    - If match count is outside [min_match, max_match], an error is returned
    - All operations in a file are validated before any are applied

    Args:
        updates: List of file update specifications. Each update must include:
            - path: File path (required)
            - overwrite: Optional string to overwrite file contents
            - update: Optional list of search/replace operations
            - rename: Optional new path (empty string to delete)
            - summary: One-sentence description of changes (required)

    Returns:
        Immediate: "OK" or error details if search/replace validations fail
        Long-term context: Combines per-file summaries
    """
    file_summaries = []
    errors = []

    for update in updates:
        file_path = update['path']
        overwrite = update.get('overwrite')
        update_ops = update.get('update')
        rename_to = update.get('rename')
        file_summary = update['summary']
        
        full_path = check_path(file_path, maca.worktree_path)
        existed_before = full_path.exists()

        # Execute operations in order: overwrite, update, rename

        # 1. Overwrite operation
        if overwrite is not None:
            # Ensure parent directory exists
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(overwrite)

        # 2. Update (search/replace) operations
        if update_ops is not None:
            if not full_path.exists():
                errors.append(f"{file_path}: Cannot apply search/replace to non-existent file")
                continue

            content = full_path.read_text()
            
            # Validate all operations first
            operation_errors = []
            for i, op in enumerate(update_ops):
                search_str = op['search']
                min_match = op.get('min_match', 1)
                max_match = op.get('max_match', 1)
                
                count = content.count(search_str)
                
                if count < min_match:
                    operation_errors.append(
                        f"  Operation {i+1}: Found {count} matches, expected at least {min_match}\n"
                        f"    Search: {json.dumps(search_str)}"
                    )
                elif count > max_match:
                    operation_errors.append(
                        f"  Operation {i+1}: Found {count} matches, expected at most {max_match}\n"
                        f"    Search: {json.dumps(search_str)}"
                    )
            
            if operation_errors:
                errors.append(f"{file_path}:\n" + "\n".join(operation_errors))
                continue
            
            # Apply all operations
            for op in update_ops:
                search_str = op['search']
                replace_str = op['replace']
                content = content.replace(search_str, replace_str)
            
            full_path.write_text(content)

        # 3. Rename operation (includes delete)
        if rename_to is not None:
            if rename_to == "":
                # Delete file
                if full_path.exists():
                    full_path.unlink()
                else:
                    errors.append(f"{file_path}: Cannot delete non-existent file")
                    continue
            else:
                # Rename/move file
                if not full_path.exists():
                    errors.append(f"{file_path}: Cannot rename non-existent file")
                    continue
                
                new_path = check_path(rename_to, maca.worktree_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.rename(new_path)

        file_summaries.append(file_summary)

    # If there were errors, return them
    if errors:
        error_msg = "Search/replace validation errors:\n\n" + "\n\n".join(errors)
        return (error_msg, "update_files: validation errors")

    # Build final summary from per-file summaries
    final_summary = "update_files: " + "; ".join(file_summaries) if file_summaries else "update_files: no changes"

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
    
    worktree = Path(maca.worktree_path)
    results = []
    content_pattern = re.compile(regex)
    files_with_matches = set()

    # Get matching files using helper
    matching_files = get_matching_files(
        worktree_path=maca.worktree_path, 
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

# IDEA:
# Only a single tool call with these arguments:
# - think_out_loud: str
# - optional file_updates: List[FileUpdate]
# - optional processors: {model: str, assignment: str, read_files: List[FileReads], shell_commands: List[ShellCommand], file_searches: List[Search]}[]
# - optional user_questions
# - result_text: str

# The 'processors' replaces file processors. It gets its own context, with its own specialized prompt (SUBPROMPT.md).
# The task can either be to..
# - make certain modifications to files based on the data
# - return a specific part of the data that was collected
# - return the answer to a question, based on the data

@tool
def shell(
    command: str,
    docker_image: str = "debian:stable",
    docker_runs: List[str] = None,
    head: int = 50,
    tail: int = 50,
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
        worktree_path=maca.worktree_path,
        repo_root=maca.repo_root,
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
                answer = pt_prompt(f"> ", history=maca.history)
            else:
                answer = result
        else:
            # Simple text input
            answer = pt_prompt(f"Question {i}/{len(questions)}: {prompt_text}\n> ", history=maca.history)
        
        answers.append(f"Q{i}: {prompt_text}\nA{i}: {answer}")
    
    result = "\n\n".join(answers)
    return (result, result)




@tool
def process_files(
    instructions: str,
    batches: List[List[Dict[str, Any]]],
    model: str = "large",
    maca = None
) -> Dict[str, Any]:
    """
    Read and process files with instructions using separate LLM calls.

    Each batch is processed with its own LLM call that has access to all tools. This ensures
    file contents never remain in the main context - they're shown once with ephemeral cache,
    then replaced with a summary.

    **Batches:**
    Each batch is a list of file specifications. Use a single batch when files need coordinated 
    changes or analysis together. Use multiple batches when making mechanical/repetitive changes 
    to many files where each file (or small group) can be processed independently.

    Batch structure - each batch is a list of file specs:
    ```
    {
        "path": "relative/path/to/file.py",
        "start_line": 10,  # Optional: first line to read (1-indexed)
        "end_line": 50     # Optional: last line to read (inclusive)
    }
    ```

    **Model Selection:**
    Choose model size based on task complexity (cost increases ~5x per size):
    - **tiny**: Simple mechanical changes (e.g., adding docstrings, formatting)
    - **small**: Straightforward refactoring (e.g., renaming variables, simple logic fixes)
    - **medium**: Moderate complexity tasks (e.g., implementing simple features, bug fixes)
    - **large** (default): Complex analysis and coordinated changes (e.g., architectural refactoring)
    - **huge**: Most complex tasks requiring deep reasoning (e.g., security audits, complex migrations)

    **Examples:**
    ```python
    # Single batch - coordinated analysis
    batches=[[{"path": "main.py"}, {"path": "utils.py"}, {"path": "config.py"}]]

    # Multiple batches - independent mechanical changes with cheap model
    batches=[[{"path": "file1.py"}], [{"path": "file2.py"}], [{"path": "file3.py"}]]
    model="tiny"

    # Batch with line ranges
    batches=[[{"path": "large_file.py", "start_line": 1, "end_line": 100}]]
    ```

    Args:
        instructions: Instructions for processing the file(s)
        batches: List of batches, where each batch is a list of file specs
        model: Model size: tiny, small, medium, large (default), huge

    Returns:
        Immediate: Dict keyed by batch index with {success: bool, result: str, cost: int, tool_called: str}
        Long-term context: Summary like "process_files: processed 5 batches (4 successful)"
    """
    if not batches:
        error_result = {"error": "No batches specified"}
        return (error_result, "process_files: error")

    # Resolve model size to actual model name
    if model not in MODELS:
        return ({"error": f"Unknown model size: {model}"}, "process_files: error")
    resolved_model = MODELS[model]

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

            full_path = check_path(file_path, maca.worktree_path)
            
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

        try:
            llm_result = call_llm(
                api_key=maca.api_key,
                model=resolved_model,
                messages=messages,
                tool_schemas=TOOL_SCHEMAS.values(),
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
            tool_result, _ = execute_tool(called_tool_name, tool_args, maca)

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
        message=f'{maca.worktree_path} -- How to proceed?',
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
        conflict = git_ops.merge_to_main(maca.repo_root, maca.worktree_path, maca.branch_name, commit_msg or result)

        if conflict:
            cprint(C_BAD, "⚠ Merge conflicts!")
            return f"Merge conflict while rebasing. Please resolve merge conflicts by reading the affected files and using update_files to resolve the conflicts. Then use shell tool with `git add <filename>.. && git rebase --continue`, before calling complete again with the same arguments to try the merge again. Here is the rebase output:\n\n{conflict}"
        
        # Cleanup
        git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
        cprint(C_GOOD, '✓ Merged and cleaned up')

        return ReadyResult(result)

    elif response == 'continue':
        feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=maca.history)
        maca.add_message({"role": "user", "content": feedback})
        return 'User rejected result and provided feedback.'
    
    elif response == 'delete':
        git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
        cprint(C_BAD, '✓ Deleted worktree and branch')
        return ReadyResult(result)
    
    else:  # cancel
        print("Keeping worktree for manual review.")
        return ReadyResult(result)


