#!/usr/bin/env python3
"""Tool system with single respond tool and processor support."""

from dataclasses import dataclass
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from typing import get_type_hints, get_origin, get_args, Any, Dict, List, Union, Optional, TypedDict
import inspect
import json
import re
import time

from utils import cprint, call_llm, get_matching_files, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO
from docker_ops import run_in_container
import git_ops


# Model size mappings for processors
MODELS = {
    'tiny': 'qwen/qwen3-coder-30b-a3b-instruct',
    'small': 'moonshotai/kimi-linear-48b-a3b-instruct',
    'medium': 'x-ai/grok-code-fast-1',
    'large': 'anthropic/claude-sonnet-4.5',
    'huge': 'anthropic/claude-opus-4.1'
}


def check_path(path: str, worktree_path: Path) -> Path:
    """
    Validate that a path is within the current directory and doesn't escape via symlinks.

    Args:
        path: The path to check (relative or absolute)
        worktree_path: The worktree path to validate against

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


# ==============================================================================
# TYPE DEFINITIONS
# ==============================================================================

class FileRead(TypedDict, total=False):
    """Specification for reading a file or file range."""
    path: str
    start_line: Optional[int]
    end_line: Optional[int]


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


class ShellCommand(TypedDict, total=False):
    """Specification for executing a shell command."""
    command: str
    docker_image: Optional[str]
    docker_runs: Optional[List[str]]
    head: Optional[int]
    tail: Optional[int]


class FileSearch(TypedDict, total=False):
    """Specification for searching file contents."""
    regex: str
    include: Optional[Union[str, List[str]]]
    exclude: Optional[Union[str, List[str]]]
    exclude_files: Optional[Union[str, List[str]]]
    max_results: Optional[int]
    lines_before: Optional[int]
    lines_after: Optional[int]


class Processor(TypedDict, total=False):
    """Specification for spawning a processor sub-context."""
    model: str
    assignment: str
    read_files: Optional[List[FileRead]]
    shell_commands: Optional[List[ShellCommand]]
    file_searches: Optional[List[FileSearch]]


class Question(TypedDict, total=False):
    """A single question with optional preset answers."""
    prompt: str
    preset_answers: Optional[List[str]]


# ==============================================================================
# TOOL SCHEMA GENERATION
# ==============================================================================

# Tool registry - single registry for all tools
TOOL_SCHEMAS = {}


def tool(func):
    """
    Decorator to register a function as an LLM tool.

    Tools are registered by name and schemas are generated on-demand
    based on the context that uses them.
    """
    TOOL_SCHEMAS[func.__name__] = generate_tool_schema(func)
    # Store function reference for execution
    func._is_tool = True
    TOOL_SCHEMAS[func.__name__]['_func'] = func
    return func


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
        if param_name in ('self', 'maca'):
            continue

        param_schema = python_type_to_json_type(type_hints.get(param_name, str))

        # Add description if available
        if param_name in param_docs:
            param_schema['description'] = param_docs[param_name]

        properties[param_name] = param_schema

        # Check if required (no default value)
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

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


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

@dataclass
class ReadyResult:
    """Signals that the task is complete and ready for merge."""
    result: Any


def apply_file_updates(updates: List[FileUpdate], worktree_path: Path) -> tuple[str, str]:
    """
    Apply file updates: create, modify, or delete files.

    Args:
        updates: List of file update specifications
        worktree_path: Path to the worktree

    Returns:
        Tuple of (immediate_result, context_summary)
        - immediate_result: "OK" or error details
        - context_summary: Combined per-file summaries
    """
    file_summaries = []
    errors = []

    for update in updates:
        file_path = update['path']
        overwrite = update.get('overwrite')
        update_ops = update.get('update')
        rename_to = update.get('rename')
        file_summary = update['summary']

        full_path = check_path(file_path, worktree_path)
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

                new_path = check_path(rename_to, worktree_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.rename(new_path)

        file_summaries.append(file_summary)

    # If there were errors, return them
    if errors:
        error_msg = "Search/replace validation errors:\n\n" + "\n\n".join(errors)
        return (error_msg, "file_updates: validation errors")

    # Build final summary from per-file summaries
    final_summary = "file_updates: " + "; ".join(file_summaries) if file_summaries else "file_updates: no changes"

    return ("OK", final_summary)


def execute_searches(searches: List[FileSearch], worktree_path: Path) -> List[Dict[str, Any]]:
    """
    Execute file searches and return results.

    Args:
        searches: List of search specifications
        worktree_path: Path to the worktree

    Returns:
        List of search results
    """
    all_results = []

    for search_spec in searches:
        regex = search_spec['regex']
        include = search_spec.get('include', '**')
        exclude = search_spec.get('exclude', '.*')
        exclude_files = search_spec.get('exclude_files')
        max_results = search_spec.get('max_results', 10)
        lines_before = search_spec.get('lines_before', 2)
        lines_after = search_spec.get('lines_after', 2)

        # Default exclude_files to ['.gitignore'] if not specified
        if exclude_files is None:
            exclude_files = ['.gitignore']

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
            rel_path_str = str(file_path.relative_to(worktree_path))

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
                            break
            except Exception:
                # Skip files that can't be read
                continue

            if len(results) >= max_results:
                break

        all_results.extend(results)

    return all_results


def execute_shell_commands(commands: List[ShellCommand], worktree_path: Path, repo_root: Path) -> List[Dict[str, Any]]:
    """
    Execute shell commands and return results.

    Args:
        commands: List of shell command specifications
        worktree_path: Path to the worktree
        repo_root: Path to the repo root

    Returns:
        List of command results
    """
    results = []

    for cmd_spec in commands:
        command = cmd_spec['command']
        docker_image = cmd_spec.get('docker_image', 'debian:stable')
        docker_runs = cmd_spec.get('docker_runs')
        head = cmd_spec.get('head', 50)
        tail = cmd_spec.get('tail', 50)

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

        results.append({
            'command': command,
            'output': result
        })

    return results


def read_files(file_specs: List[FileRead], worktree_path: Path) -> List[str]:
    """
    Read files and return their contents.

    Args:
        file_specs: List of file read specifications
        worktree_path: Path to the worktree

    Returns:
        List of file contents (formatted with path and optional line range)
    """
    contents = []

    for file_spec in file_specs:
        file_path = file_spec['path']
        start_line = file_spec.get('start_line')
        end_line = file_spec.get('end_line')

        full_path = check_path(file_path, worktree_path)

        if not full_path.exists():
            contents.append(f"File: {file_path}\n\nError: File not found")
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
                contents.append(
                    f"File: {file_path} (lines {start_idx + 1}-{end_idx})\n\n{data}"
                )
            else:
                data = ''.join(lines)
                contents.append(f"File: {file_path}\n\n{data}")
        except Exception as e:
            contents.append(f"File: {file_path}\n\nError: {str(e)}")

    return contents


def ask_questions(questions: List[Question], history) -> str:
    """
    Ask the user questions and return answers.

    Args:
        questions: List of question specifications
        history: FileHistory for prompt_toolkit

    Returns:
        Formatted string with all Q&A pairs
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
    return result


def execute_processor(processor: Processor, maca, subprompt: str) -> str:
    """
    Execute a processor in its own context.

    Args:
        processor: Processor specification
        maca: MACA instance
        subprompt: System prompt for the processor

    Returns:
        The result_text from the processor's respond call
    """
    model_name = processor.get('model', 'large')
    assignment = processor['assignment']
    read_files_specs = processor.get('read_files', [])
    shell_commands_specs = processor.get('shell_commands', [])
    file_searches_specs = processor.get('file_searches', [])

    # Resolve model name
    if model_name not in MODELS:
        raise ValueError(f"Unknown model size: {model_name}")
    resolved_model = MODELS[model_name]

    cprint(C_INFO, f'  Spawning processor with model={model_name}')

    # Build processor context
    messages = [
        {'role': 'system', 'content': subprompt}
    ]

    # Execute data gathering operations
    data_parts = []

    # Read files
    if read_files_specs:
        file_contents = read_files(read_files_specs, maca.worktree_path)
        data_parts.append("# Files\n\n" + "\n\n---\n\n".join(file_contents))

    # Execute shell commands
    if shell_commands_specs:
        shell_results = execute_shell_commands(shell_commands_specs, maca.worktree_path, maca.repo_root)
        shell_output = []
        for result in shell_results:
            shell_output.append(f"Command: {result['command']}\n\n{json.dumps(result['output'], indent=2)}")
        data_parts.append("# Shell Commands\n\n" + "\n\n---\n\n".join(shell_output))

    # Execute searches
    if file_searches_specs:
        search_results = execute_searches(file_searches_specs, maca.worktree_path)
        search_output = json.dumps(search_results, indent=2)
        data_parts.append(f"# Search Results\n\n{search_output}")

    # Combine all data
    if data_parts:
        combined_data = "\n\n===\n\n".join(data_parts)
    else:
        combined_data = "(no data gathered)"

    # Add assignment and data to processor context
    messages.append({
        'role': 'user',
        'content': f"# Assignment\n\n{assignment}\n\n# Data\n\n{combined_data}"
    })

    # Call LLM for processor
    try:
        llm_result = call_llm(
            api_key=maca.api_key,
            model=resolved_model,
            messages=messages,
            tool_schemas=list(TOOL_SCHEMAS.values()),
        )

        message = llm_result['message']

        # Extract and execute tool call
        tool_calls = message.get('tool_calls', [])
        if not tool_calls:
            return "Error: Processor did not make a tool call"

        tool_call = tool_calls[0]
        tool_name = tool_call['function']['name']
        tool_args = json.loads(tool_call['function']['arguments'])

        # Processor should call respond
        if tool_name != 'respond':
            return f"Error: Processor called {tool_name} instead of respond"

        # Handle processor's file_updates if present
        if 'file_updates' in tool_args and tool_args['file_updates']:
            immediate_result, _ = apply_file_updates(tool_args['file_updates'], maca.worktree_path)
            if immediate_result != "OK":
                return f"Error in processor file_updates: {immediate_result}"

        # Return the result_text
        return tool_args.get('result_text', '')

    except Exception as e:
        return f"Error: Processor execution failed: {str(e)}"


# ==============================================================================
# MAIN TOOL
# ==============================================================================

@tool
def respond(
    think_out_loud: str,
    result_text: str,
    file_updates: Optional[List[FileUpdate]] = None,
    processors: Optional[List[Processor]] = None,
    user_questions: Optional[List[Question]] = None,
    complete: bool = False,
    maca = None
) -> tuple[str, str]:
    """
    Single tool for responding to user requests.

    This is the only tool available. Use it to:
    - Think through the problem (think_out_loud)
    - Update files (file_updates)
    - Spawn processors for data gathering (processors)
    - Ask the user questions (user_questions)
    - Report results (result_text)
    - Signal completion (complete)

    Args:
        think_out_loud: Brief reasoning about what you're doing (max 100 words)
        result_text: What to report back to context (summary of work done, findings, etc.)
        file_updates: Optional list of file modifications to apply
        processors: Optional list of processor sub-contexts to spawn for data gathering
        user_questions: Optional list of questions to ask the user
        complete: Set to true when task is fully complete and ready for merge

    Returns:
        Immediate: Full details of what happened
        Long-term context: Brief summary for context
    """
    immediate_parts = []
    context_parts = []

    # 1. Handle file updates
    if file_updates:
        immediate_result, context_summary = apply_file_updates(file_updates, maca.worktree_path)
        immediate_parts.append(f"File Updates: {immediate_result}")
        context_parts.append(context_summary)

        if immediate_result != "OK":
            # File update failed, return early
            immediate_output = "\n\n".join(immediate_parts)
            context_output = "; ".join(context_parts)
            return (immediate_output, context_output)

    # 2. Handle processors
    if processors:
        # Load subprompt
        script_dir = Path(__file__).parent
        subprompt_path = script_dir / 'SUBPROMPT.md'

        if not subprompt_path.exists():
            return ("Error: SUBPROMPT.md not found", "respond: error")

        subprompt = subprompt_path.read_text()

        processor_results = []
        for i, processor in enumerate(processors):
            cprint(C_INFO, f'  [{i + 1}/{len(processors)}] Executing processor')
            result = execute_processor(processor, maca, subprompt)
            processor_results.append(result)

        immediate_parts.append(f"Processor Results:\n" + "\n\n---\n\n".join(processor_results))
        context_parts.append(f"processors: executed {len(processors)}")

    # 3. Handle user questions
    if user_questions:
        answers = ask_questions(user_questions, maca.history)
        immediate_parts.append(f"User Answers:\n{answers}")
        context_parts.append(f"user_questions: asked {len(user_questions)} questions")

    # 4. Handle completion
    if complete:
        cprint(C_GOOD, '\n✓ Task completed!\n\n', C_NORMAL, result_text, '\n')

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

            # Extract commit message from result_text (first line)
            commit_msg = result_text.split('\n')[0]

            # Merge
            conflict = git_ops.merge_to_main(maca.repo_root, maca.worktree_path, maca.branch_name, commit_msg)

            if conflict:
                cprint(C_BAD, "⚠ Merge conflicts!")
                return (f"Merge conflict while rebasing. Please resolve merge conflicts by reading the affected files and using file_updates to resolve the conflicts. Then use a processor with shell_command to run `git add <filename>.. && git rebase --continue`, before calling respond again with complete=true to try the merge again. Here is the rebase output:\n\n{conflict}",
                        "respond: merge conflict")

            # Cleanup
            git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
            cprint(C_GOOD, '✓ Merged and cleaned up')

            return (ReadyResult(result_text), "respond: completed and merged")

        elif response == 'continue':
            feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=maca.history)
            maca.add_message({"role": "user", "content": feedback})
            return ('User rejected completion and provided feedback.', 'respond: completion rejected')

        elif response == 'delete':
            git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
            cprint(C_BAD, '✓ Deleted worktree and branch')
            return (ReadyResult(result_text), "respond: completed and deleted")

        else:  # cancel
            print("Keeping worktree for manual review.")
            return (ReadyResult(result_text), "respond: completed")

    # Build final output
    if immediate_parts:
        immediate_output = "\n\n".join(immediate_parts)
    else:
        immediate_output = "OK"

    if context_parts:
        context_output = "respond: " + "; ".join(context_parts)
    else:
        context_output = "respond: executed"

    return (immediate_output, context_output)
