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
import fnmatch

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


class SubProcessor(TypedDict, total=False):
    """Specification for spawning a processor sub-context."""
    model: str
    assignment: str
    file_reads: Optional[List[FileRead]]
    file_write_allow_globs: Optional[List[str]] # List of globs that may be written to (default to none)


class Question(TypedDict, total=False):
    """A single question with optional preset answers."""
    prompt: str
    preset_answers: Optional[List[str]]


# ==============================================================================
# TOOL SCHEMA GENERATION
# ==============================================================================

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
    errors = []

    for update in updates:
        file_path = update['path']
        overwrite = update.get('overwrite')
        update_ops = update.get('update')
        rename_to = update.get('rename')

        full_path = check_path(file_path, worktree_path)

        # Execute operations in order: overwrite, update, rename

        # 1. Overwrite operation
        if overwrite is not None:
            # Ensure parent directory exists
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(overwrite)

        # 2. Update (search/replace) operations
        if update_ops is not None:
            if not full_path.exists():
                errors.append({"error": "Cannot apply search/replace to non-existent file", "path": file_path})
                continue

            content = full_path.read_text()

            # Validate all operations first
            for i, op in enumerate(update_ops):
                search_str = op['search']
                min_match = op.get('min_match', 1)
                max_match = op.get('max_match', 1)

                count = content.count(search_str)
                if count < min_match:
                    errors.append({"error": "Too few matches", "path": file_path, "index": i, "search": search_str, "match_count": count, "min_match": min_match, "partial": "Other replace blocks may have succeeded"})
                elif count > max_match:
                    errors.append({"error": "Too many matches", "path": file_path, "index": i, "search": search_str, "match_count": count, "max_match": max_match, "partial": "Other replace blocks may have succeeded"})
                else:
                    content = content.replace(search_str, op['replace'])

            try:
                full_path.write_text(content)
            except Exception as e:
                errors.append({"error": f"Failed to write updated content: {str(e)}", "path": file_path})
                continue

        # 3. Rename operation (includes delete)
        if rename_to is not None:
            if rename_to == "":
                # Delete file
                if full_path.exists():
                    full_path.unlink()
                else:
                    errors.append({"error": "Cannot delete non-existent file", "path": file_path})
                    continue
            else:
                # Rename/move file
                if not full_path.exists():
                    errors.append({"error": "Cannot rename non-existent file", "path": file_path})
                    continue

                new_path = check_path(rename_to, worktree_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                full_path.rename(new_path)

    # If there were errors, return them
    return errors


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

        results.append(result)

    return results


def read_files(file_specs: List[FileRead], worktree_path: Path) -> List[str]:
    """
    Read files and return their contents.

    Args:
        file_specs: List of file read specifications
        worktree_path: Path to the worktree

    Returns:
        List of file contents
    """
    contents = {}

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
                contents[file_path] = {"data": data, "lines": f"{start_idx + 1}-{end_idx}"}
            else:
                data = ''.join(lines)
                contents[file_path] = {"data": data}
        except Exception as e:
            contents[file_path] = {"error": str(e)}

    return contents


def ask_questions(questions: List[Question], maca) -> None:
    """
    Ask questions to the user and store Q&A in context.

    Args:
        questions: List of questions to ask
        maca: MACA instance
    """
    for i, q in enumerate(questions, 1):
        prompt_text = q.get('prompt', '')
        preset_answers = q.get('preset_answers')

        # Handle non-interactive mode
        if maca.non_interactive:
            answer = "This agent is running non-interactively. Please try to take a guess at the answer yourself, but be a bit conservative and refuse the assignment if needed."
            cprint(C_INFO, f"Question {i}/{len(questions)}: {prompt_text}")
            cprint(C_INFO, f"Auto-response: {answer}")
        elif preset_answers:
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

        maca.add_message({'role': 'assistant', 'content': prompt_text})
        maca.add_message({'role': 'user', 'content': answer})


def run_subprocessor(processor: SubProcessor, maca, system_prompt: str) -> str:
    """
    Execute a processor in its own context.

    Args:
        processor: Processor specification
        maca: MACA instance
        subprompt: System prompt for the processor

    Returns:
        The result from the processor's respond call
    """
    model_name = processor.get('model', 'large')
    assignment = processor['assignment']
    file_reads_specs = processor.get('file_reads', [])
    file_write_allow_globs = processor.get('file_write_allow_globs', [])

    # Resolve model name
    if model_name not in MODELS:
        raise ValueError(f"Unknown model size: {model_name}")
    resolved_model = MODELS[model_name]

    cprint(C_INFO, f'  Spawning processor with model={model_name}')

    # Build processor context
    prompt = f"# Assignment\n\n{assignment}"

    if file_write_allow_globs:
        prompt = prompt + "\n\n# Allowed globs for update_files:\n\n" + json.dumps(file_write_allow_globs) 

    # Read files if specified
    if file_reads_specs:
        prompt = "# Files\n\n" + json.dumps(read_files(file_reads_specs, maca.worktree_path)) + "\n\n" + prompt

    # Add assignment and data to processor context
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': prompt},
    ]

    # Call LLM for processor
    try:
        llm_result = call_llm(
            api_key=maca.api_key,
            model=resolved_model,
            messages=messages,
            tool_schemas=[SUBPROCESSOR_RESPOND_TOOL_SCHEMA if file_write_allow_globs else SUBPROCESSOR_RESPOND_NO_UPDATES_TOOL_SCHEMA],
        )

        message = llm_result['message']

        # Extract and execute tool call
        tool_calls = message.get('tool_calls', [])
        if not tool_calls:
            return "Error: Processor did not make a tool call"

        tool_call = tool_calls[0]
        tool_name = tool_call['function']['name']
        tool_args = json.loads(tool_call['function']['arguments'])

        # Processor should call subprocessor_respond
        if tool_name != 'subprocessor_respond':
            return f"Error: Processor called {tool_name} instead of subprocessor_respond"

        # Handle processor's file_updates if present (with write pattern validation)
        if 'file_updates' in tool_args and tool_args['file_updates']:
            # Validate file paths against allow patterns
            file_updates = []
            for update in tool_args['file_updates']:
                file_path = update['path']
                allowed = any(fnmatch.fnmatch(file_path, pattern) for pattern in file_write_allow_globs)
                if allowed:
                    file_updates.append(update)
                else:
                    errors({"error": f"Processor tried to write to '{file_path}' which doesn't match allowed globs", "path": file_path})

            errors = apply_file_updates(tool_args['file_updates'], maca.worktree_path)
            if errors:
                messages.append({
                    'role': 'user',
                    'content': [{
                        'type': 'tool_result',
                        'tool_use_id': tool_calls[0]['id'],
                        'content': json.dumps({"file_update_errors": errors, "proceed": "Carefully retry just the rejected file updates"})
                    }]
                })

            if errors:
                return json.dumps(errors)

        # Return the result
        return tool_args.get('result', '')

    except Exception as e:
        return f"Error: Processor execution failed: {str(e)}"


# ==============================================================================
# SUBPROCESSOR TOOL
# ==============================================================================


def subprocessor_respond(
    thoughts: str,
    file_updates: Optional[List[FileUpdate]] = None,
    result: str = ""
):
    """
    Mandatory tool to use.

    Args:
        thoughts: Your reasoning about the task. Not saved or shown to user.
        file_updates: Optional list of file modifications to apply.
        result: The result to return to the main context. This should contain your findings,
                analysis, or completion status. Be concise but complete.
    """
    # This function signature is used for schema generation only.
    # Actual execution is handled in execute_processor()
    pass

def subprocessor_respond_no_updates(
    thoughts: str,
    result: str = ""
):
    """
    Mandatory tool to use.

    Args:
        thoughts: Your reasoning about the task. Not saved or shown to user.
        result: The result to return to the main context. This should contain your findings,
                analysis, or completion status. Be concise but complete.
    """
    # This function signature is used for schema generation only.
    # Actual execution is handled in execute_processor()
    pass

# ==============================================================================
# MAIN TOOL
# ==============================================================================

def respond(
    thoughts: str,
    keep_extended_context: Optional[bool] = None,
    file_updates: Optional[List[FileUpdate]] = None,
    user_questions: Optional[List[Question]] = None,
    file_reads: Optional[List[FileRead]] = None,
    file_searches: Optional[List[FileSearch]] = None,
    shell_commands: Optional[List[ShellCommand]] = None,
    sub_processors: Optional[List[SubProcessor]] = None,
    file_change_description: Optional[str] = None,
    notes_for_context: Optional[str] = None,
    done: Optional[bool] = None,
    user_output: Optional[str] = None,

    maca = None,
) -> Dict[str, Any]:
    """
    Mandatory tool to call. Operations should be combined as much as possible. If you currently cannot complete the task, try to gather all info via user_questions, file_reads, file_searches, and shell_commands such that you can complete the entire task/subtask in the next call.

    Args:
        thoughts: Reason for yourself about what you need to do. This will not be saved in the context nor shown to the user. Be succinct (sacrifice grammar for briefness) and self-critical.
        keep_extended_context: Set to true if you need to preserve the temporary context (full results from file_reads, file_searches, shell_commands, sub_processors) for the iteration. Do this only if you just found out that you are missing some information to accomplish a task that requires access to those results. If it's possible to densely summarize the info you'll still need in the future to `notes_for_context`, do that instead.
        file_updates: Optional list of file modifications to apply. Executed FIRST (before data gathering operations).
        user_questions: Optional list of questions to ask the user. Answers are stored in long-term context. If `preset_answers` is given, a 'other' option is always added automatically.
        file_reads: Optional list of file (parts) to read into temporary context.
        file_searches: Optional list of file searches to read into temporary context.
        shell_commands: Optional list of shell commands to execute. Stdout and stderr go into temporary context. Don't even use shell commands to write files - use file_updates for that!
        sub_processors: Optional list of LLM-calls (in a a fresh context) to perform. These will be single-shots. They can only update files and return a result. This can be very useful to:
          - Ask a cheaper LLM to do some file edits.
          - Split up independent work to different files into parallel LLM calls.
          - Ask a more capable LLM for help on a hard problem.
          - Ask a web-search-enabled LLM to look up info online.
          The `model` field can be one of: small, medium, large, huge or search. The `assignment` should contain a full briefing - the sub-processor has no access to prior context. The `file_reads` field can be used to provide input data files. Specify `file_reads` to pass file contents into the sub-processor's context. The `file_write_allow_globs` field can be used to give the model write access to certain files. If left empty, no files can be updated. 
          The result goes into our temporary context.
        file_change_description: An optional brief (one sentence) description of the purpose of file changes made by file_updates, shell_commands and/or sub_processors. This will be used as the git commit message.
        notes_for_context: Succinct (sacrifice grammar for briefness): A brief summary of...
          - Your thoughts (if valuable)
          - Temporary context that will be lost (all file_reads+file_searches+shell_commands+sub_processors unless keep_extended_context is set), insofar that you'll need it later in the conversation
          - What the next completion needs to do with the input data it receives (unless obvious)
          This will be saved in long-term context.
        user_output: Optionally what to show to the user. Don't provide this, unless there is something interesting and new to tell. If `done` is true, you *must* provide a meaningful answer here. Keep it short.
        done: Set to true if you have completed the overall task given by the user. This will trigger the merge process. Make sure everything is done, nice and clean before you set this! If set, further operations (file_reads, file_searches, shell_commands, sub_processors) are not allowed. Also, you *must* provide a meaningful `user_output` answering the user question or briefly summarizing the work done.

    Returns:
        Tuple of (temporary_response, done):
        - temporary_response: Dict with full operation results including all data
        - done: Whether the task is complete
    """

    # Build response structures
    response = {}
    if done is None:
        done = False

    # Validate done constraints
    if done:
        if not user_output:
            response["done"] = "Error: 'done' is set but 'user_output' is missing. You must provide a meaningful summary when marking the task as complete."
            done = False

        if any([file_reads, file_searches, shell_commands, sub_processors]):
            response["done"] = "Error: 'done' is set but data gathering operations (file_reads, file_searches, shell_commands, sub_processors) are not allowed when done=True."
            done = False

    # 1. Handle file updates FIRST (LLM has already output the write comments)
    if file_updates:
        # Print file updates summary
        for update in file_updates:
            path = update['path']
            if update.get('overwrite') is not None:
                line_count = len(update['overwrite'].splitlines())
                cprint(C_INFO, f"Writing {path} ({line_count} lines)")
            elif update.get('update') is not None:
                cprint(C_INFO, f"Updating {path} ({len(update['update'])} operations)")
            elif update.get('rename') is not None:
                if update['rename'] == "":
                    cprint(C_INFO, f"Deleting {path}")
                else:
                    cprint(C_INFO, f"Renaming {path} -> {update['rename']}")

        response['file_updates'] = apply_file_updates(file_updates, maca.worktree_path) or 'OK'
        if response['file_updates'] != 'OK':
            done = False

    # 2. Handle user questions (to get input before data gathering operations)
    if user_output:
        cprint(C_GOOD, user_output)
    if user_questions:
        # Q&A are stored as separate messages in long-term context
        ask_questions(user_questions, maca)

    # 3. Handle file reads
    if file_reads:
        # Print concise summary
        file_list = ', '.join(fr['path'] for fr in file_reads)
        cprint(C_INFO, f"Reading {len(file_reads)} file(s): {file_list}")

        response['file_reads'] = read_files(file_reads, maca.worktree_path)

    # 4. Handle file searches
    if file_searches:
        # Print search summary
        for search in file_searches:
            cprint(C_INFO, f"Searching for /{search['regex']}/")

        response['file_searches'] = execute_searches(file_searches, maca.worktree_path)

    # 5. Handle shell commands
    if shell_commands:
        # Print each command upfront
        for cmd_spec in shell_commands:
            cprint(C_INFO, f"Running: {cmd_spec['command']}")

        response['shell_commands'] = execute_shell_commands(shell_commands, maca.worktree_path, maca.repo_root)

    # Commit changes if files were updated
    if maca.last_head_commit != git_ops.get_head_commit(maca.worktree_path):
        git_ops.commit_changes(maca.worktree_path, f"MACA: {file_change_description or 'No description'}")
        maca.last_head_commit = git_ops.get_head_commit(maca.worktree_path)

    # 6. Handle sub-processors
    if sub_processors:
        # Load subprompt
        script_dir = Path(__file__).parent
        subprompt_path = script_dir / 'subprompt.md'
        subprompt = subprompt_path.read_text()

        processor_results = []
        for i, processor in enumerate(sub_processors):
            cprint(C_INFO, f'  [{i + 1}/{len(sub_processors)}] Executing processor')
            result = run_subprocessor(processor, maca, subprompt)
            processor_results.append(result)

        response['sub_processors'] = processor_results

    # 9. Manage context cleanup
    if not keep_extended_context:
        maca.clear_temporary_messages()

    return (response, done)


RESPOND_TOOL_SCHEMA = generate_tool_schema(respond)
SUBPROCESSOR_RESPOND_TOOL_SCHEMA = generate_tool_schema(subprocessor_respond)
SUBPROCESSOR_RESPOND_NO_UPDATES_TOOL_SCHEMA = generate_tool_schema(subprocessor_respond_no_updates)
