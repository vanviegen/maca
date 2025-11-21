#!/usr/bin/env python3
"""Command execution system for MACA."""

from dataclasses import dataclass
from pathlib import Path
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from typing import Dict, List, Optional, Any
import fnmatch
import re
import json

from utils import cprint, get_matching_files, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO
from llm import call_llm
from docker_ops import run_in_container
from command_parser import Command, parse_commands, format_command_results, get_cancelled_ids
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
# HELPER FUNCTIONS
# ==============================================================================

def apply_single_update(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Apply a single UPDATE command (search/replace).

    Args:
        cmd: Command with search, replace, min_match, max_match args
        worktree_path: Path to the worktree

    Returns:
        Result dict
    """
    path = cmd.args.get('path')
    search = cmd.args.get('search')
    replace = cmd.args.get('replace')
    min_match = int(cmd.args.get('min_match', '1'))
    max_match = int(cmd.args.get('max_match', '1'))

    if not path or not search:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required args: path, search'}

    if replace is None:
        replace = ''

    try:
        full_path = check_path(path, worktree_path)
    except ValueError as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e)}

    if not full_path.exists():
        return {'id': cmd.id, 'status': 'error', 'error': 'File not found', 'path': path}

    try:
        content = full_path.read_text()
        count = content.count(search)

        if count < min_match:
            return {
                'id': cmd.id,
                'status': 'error',
                'error': 'Too few matches',
                'path': path,
                'match_count': count,
                'min_match': min_match,
                'search': search[:100]  # Truncate for readability
            }
        elif count > max_match:
            return {
                'id': cmd.id,
                'status': 'error',
                'error': 'Too many matches',
                'path': path,
                'match_count': count,
                'max_match': max_match,
                'search': search[:100]
            }
        else:
            content = content.replace(search, replace)
            full_path.write_text(content)
            return {
                'id': cmd.id,
                'status': 'success',
                'path': path,
                'replacements': count
            }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e), 'path': path}


def apply_overwrite(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Apply OVERWRITE command (write entire file).

    Args:
        cmd: Command with path and content args
        worktree_path: Path to the worktree

    Returns:
        Result dict
    """
    path = cmd.args.get('path')
    content = cmd.args.get('content', '')

    if not path:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: path'}

    try:
        full_path = check_path(path, worktree_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

        line_count = len(content.splitlines())
        cprint(C_INFO, f"Writing {path} ({line_count} lines)")

        return {
            'id': cmd.id,
            'status': 'success',
            'path': path,
            'lines': line_count
        }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e), 'path': path}


def apply_rename(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Apply RENAME command (move file).

    Args:
        cmd: Command with old_path and new_path args
        worktree_path: Path to the worktree

    Returns:
        Result dict
    """
    old_path = cmd.args.get('old_path')
    new_path = cmd.args.get('new_path')

    if not old_path or not new_path:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required args: old_path, new_path'}

    try:
        old_full = check_path(old_path, worktree_path)
        new_full = check_path(new_path, worktree_path)

        if not old_full.exists():
            return {'id': cmd.id, 'status': 'error', 'error': 'File not found', 'path': old_path}

        new_full.parent.mkdir(parents=True, exist_ok=True)
        old_full.rename(new_full)

        cprint(C_INFO, f"Renamed {old_path} -> {new_path}")

        return {
            'id': cmd.id,
            'status': 'success',
            'old_path': old_path,
            'new_path': new_path
        }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e)}


def apply_delete(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Apply DELETE command.

    Args:
        cmd: Command with path arg
        worktree_path: Path to the worktree

    Returns:
        Result dict
    """
    path = cmd.args.get('path')

    if not path:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: path'}

    try:
        full_path = check_path(path, worktree_path)

        if not full_path.exists():
            return {'id': cmd.id, 'status': 'error', 'error': 'File not found', 'path': path}

        full_path.unlink()

        cprint(C_INFO, f"Deleted {path}")

        return {
            'id': cmd.id,
            'status': 'success',
            'path': path
        }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e), 'path': path}


def execute_read(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Execute READ command.

    Args:
        cmd: Command with path, start_line, end_line args
        worktree_path: Path to the worktree

    Returns:
        Result dict with file contents
    """
    path = cmd.args.get('path')
    start_line = cmd.args.get('start_line')
    end_line = cmd.args.get('end_line')

    if not path:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: path'}

    try:
        full_path = check_path(path, worktree_path)

        if not full_path.exists():
            return {'id': cmd.id, 'status': 'error', 'error': 'File not found', 'path': path}

        with open(full_path, 'r') as f:
            lines = f.readlines()

        # Handle line range
        if start_line or end_line:
            start_idx = (int(start_line) - 1) if start_line else 0
            end_idx = int(end_line) if end_line else len(lines)
            selected_lines = lines[start_idx:end_idx]
            data = ''.join(selected_lines)
            line_range = f"{start_idx + 1}-{end_idx}"
        else:
            data = ''.join(lines)
            line_range = f"1-{len(lines)}"

        cprint(C_INFO, f"Reading {path} (lines {line_range})")

        return {
            'id': cmd.id,
            'status': 'success',
            'path': path,
            'lines': line_range,
            'line_count': len(data.splitlines()),
            'data': data
        }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e), 'path': path}


def execute_search(cmd: Command, worktree_path: Path) -> Dict[str, Any]:
    """
    Execute SEARCH command.

    Args:
        cmd: Command with regex and other search args
        worktree_path: Path to the worktree

    Returns:
        Result dict with search results
    """
    regex = cmd.args.get('regex')
    include = cmd.args.get('include', '**')
    exclude = cmd.args.get('exclude', '.*')
    exclude_files = cmd.args.get('exclude_files', '.gitignore')
    max_results = int(cmd.args.get('max_results', '10'))
    lines_before = int(cmd.args.get('lines_before', '2'))
    lines_after = int(cmd.args.get('lines_after', '2'))

    if not regex:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: regex'}

    try:
        content_pattern = re.compile(regex)
    except re.error as e:
        return {'id': cmd.id, 'status': 'error', 'error': f'Invalid regex: {e}'}

    # Parse exclude_files
    if isinstance(exclude_files, str):
        exclude_files = [exclude_files] if exclude_files else []

    cprint(C_INFO, f"Searching for /{regex}/")

    results = []
    files_with_matches = set()

    # Get matching files using helper
    try:
        matching_files = get_matching_files(
            worktree_path=worktree_path,
            include=include,
            exclude=exclude,
            exclude_files=exclude_files
        )
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e)}

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
                        'context': context
                    })
                    files_with_matches.add(rel_path_str)

                    if len(results) >= max_results:
                        break
        except Exception:
            # Skip files that can't be read
            continue

        if len(results) >= max_results:
            break

    return {
        'id': cmd.id,
        'status': 'success',
        'regex': regex,
        'match_count': len(results),
        'file_count': len(files_with_matches),
        'files': sorted(files_with_matches),
        'matches': results
    }


def execute_shell(cmd: Command, worktree_path: Path, repo_root: Path) -> Dict[str, Any]:
    """
    Execute SHELL command.

    Args:
        cmd: Command with command and docker args
        worktree_path: Path to the worktree
        repo_root: Path to the repo root

    Returns:
        Result dict with command output
    """
    command = cmd.args.get('command')
    docker_image = cmd.args.get('docker_image', 'debian:stable')
    docker_runs = cmd.args.get('docker_runs', '')
    head = int(cmd.args.get('head', '50'))
    tail = int(cmd.args.get('tail', '50'))

    if not command:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: command'}

    # Parse docker_runs (could be JSON list or newline-separated)
    if docker_runs:
        if docker_runs.startswith('['):
            docker_runs = json.loads(docker_runs)
        else:
            docker_runs = [line.strip() for line in docker_runs.split('\n') if line.strip()]
    else:
        docker_runs = []

    cprint(C_INFO, f"Running: {command}")

    try:
        result = run_in_container(
            command=command,
            worktree_path=worktree_path,
            repo_root=repo_root,
            docker_image=docker_image,
            docker_runs=docker_runs,
            head=head,
            tail=tail
        )

        return {
            'id': cmd.id,
            'status': 'success',
            'command': command,
            'exit_code': result.get('exit_code', 0),
            'output': result.get('output', '')
        }
    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': str(e), 'command': command}


def execute_process(cmd: Command, maca) -> Dict[str, Any]:
    """
    Execute PROCESS command (spawn subprocessor).

    Args:
        cmd: Command with model, assignment, and other args
        maca: MACA instance

    Returns:
        Result dict with processor result
    """
    model_name = cmd.args.get('model', 'large')
    assignment = cmd.args.get('assignment')

    if not assignment:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: assignment'}

    # Resolve model name
    if model_name not in MODELS:
        return {'id': cmd.id, 'status': 'error', 'error': f'Unknown model: {model_name}'}

    resolved_model = MODELS[model_name]

    cprint(C_INFO, f'  Spawning processor with model={model_name}')

    # Build processor context
    prompt = f"# Assignment\n\n{assignment}"

    # Add file_write_allow_globs if specified
    file_write_allow_globs = cmd.args.get('file_write_allow_globs', '')
    if file_write_allow_globs:
        if file_write_allow_globs.startswith('['):
            globs = json.loads(file_write_allow_globs)
        else:
            globs = [g.strip() for g in file_write_allow_globs.split('\n') if g.strip()]
        prompt += f"\n\n# Allowed file write globs:\n\n{json.dumps(globs)}"
    else:
        globs = []

    # Read files if specified
    file_reads = cmd.args.get('file_reads', '')
    if file_reads:
        if file_reads.startswith('['):
            reads = json.loads(file_reads)
        else:
            reads = [{'path': p.strip()} for p in file_reads.split('\n') if p.strip()]

        # Read files
        file_contents = {}
        for read_spec in reads:
            path = read_spec.get('path')
            if path:
                try:
                    full_path = check_path(path, maca.worktree_path)
                    if full_path.exists():
                        file_contents[path] = full_path.read_text()
                    else:
                        file_contents[path] = f"ERROR: File not found"
                except Exception as e:
                    file_contents[path] = f"ERROR: {e}"

        prompt = f"# Files\n\n{json.dumps(file_contents, indent=2)}\n\n" + prompt

    # Load subprompt
    script_dir = Path(__file__).parent
    subprompt_path = script_dir / 'subprompt.md'
    subprompt = subprompt_path.read_text()

    # Add assignment and data to processor context
    messages = [
        {'role': 'system', 'content': subprompt},
        {'role': 'user', 'content': prompt},
    ]

    # Call LLM for processor (no tool schemas - just text output)
    try:
        llm_result = call_llm(
            model=resolved_model,
            messages=messages,
            tool_schemas=None,
        )

        message = llm_result['message']
        text_content = message.get('content', '')

        # Parse commands from processor output
        parse_result = parse_commands(text_content)
        cancelled_ids = get_cancelled_ids(parse_result.commands)

        # Execute processor commands (limited set)
        processor_results = []
        for proc_cmd in parse_result.commands:
            if proc_cmd.id in cancelled_ids:
                continue

            if proc_cmd.command == 'OVERWRITE':
                # Check against allowed globs
                path = proc_cmd.args.get('path')
                if globs and path:
                    allowed = any(fnmatch.fnmatch(path, pattern) for pattern in globs)
                    if not allowed:
                        processor_results.append({
                            'id': proc_cmd.id,
                            'status': 'error',
                            'error': f"Path '{path}' not allowed by write globs"
                        })
                        continue
                processor_results.append(apply_overwrite(proc_cmd, maca.worktree_path))

            elif proc_cmd.command == 'UPDATE':
                # Check against allowed globs
                path = proc_cmd.args.get('path')
                if globs and path:
                    allowed = any(fnmatch.fnmatch(path, pattern) for pattern in globs)
                    if not allowed:
                        processor_results.append({
                            'id': proc_cmd.id,
                            'status': 'error',
                            'error': f"Path '{path}' not allowed by write globs"
                        })
                        continue
                processor_results.append(apply_single_update(proc_cmd, maca.worktree_path))

            elif proc_cmd.command == 'OUTPUT':
                # Collect output (this is the result)
                pass

        # Extract OUTPUT command result
        output_cmds = [c for c in parse_result.commands if c.command == 'OUTPUT']
        if output_cmds:
            result_text = output_cmds[0].args.get('text', '')
        else:
            result_text = parse_result.thinking[:500] if parse_result.thinking else "No output"

        return {
            'id': cmd.id,
            'status': 'success',
            'model': model_name,
            'result': result_text,
            'commands_executed': len(processor_results)
        }

    except Exception as e:
        return {'id': cmd.id, 'status': 'error', 'error': f'Processor failed: {str(e)}'}


def execute_ask(cmd: Command, maca) -> Dict[str, Any]:
    """
    Execute ASK command (ask user a question).

    Args:
        cmd: Command with question and option args
        maca: MACA instance

    Returns:
        Result dict
    """
    question = cmd.args.get('question')

    if not question:
        return {'id': cmd.id, 'status': 'error', 'error': 'Missing required arg: question'}

    # Collect preset options
    options = []
    i = 1
    while f'option{i}' in cmd.args:
        options.append(cmd.args[f'option{i}'])
        i += 1

    # Handle non-interactive mode
    if maca.non_interactive:
        answer = "This agent is running non-interactively. Please try to take a guess at the answer yourself, but be a bit conservative and refuse the assignment if needed."
        cprint(C_INFO, f"Question: {question}")
        cprint(C_INFO, f"Auto-response: {answer}")
    elif options:
        # Show choice selection
        choices = [(opt, opt) for opt in options]
        choices.append(('__custom__', 'Other (custom input)'))

        result = choice(
            message=f"Question: {question}",
            options=choices
        )

        if result == '__custom__':
            answer = pt_prompt(f"> ", history=maca.history)
        else:
            answer = result
    else:
        # Simple text input
        answer = pt_prompt(f"Question: {question}\n> ", history=maca.history)

    # Add Q&A to context
    maca.add_message({'role': 'assistant', 'content': question})
    maca.add_message({'role': 'user', 'content': answer})

    return {
        'id': cmd.id,
        'status': 'success',
        'question': question,
        'answer': answer
    }


def execute_commands(text: str, maca) -> tuple[List[Dict], List[Dict], bool, str]:
    """
    Parse and execute commands from LLM text output.

    Args:
        text: Raw text from LLM
        maca: MACA instance

    Returns:
        Tuple of (temporary_results, long_term_results, done, thinking)
        - temporary_results: Full command results with data
        - long_term_results: Metadata only (data replaced with OMITTED)
        - done: Whether task is complete (no pending operations)
        - thinking: Non-command text from LLM
    """
    # Parse commands
    parse_result = parse_commands(text)
    cancelled_ids = get_cancelled_ids(parse_result.commands)

    # Track state
    temporary_results = []
    keep_context = False
    notes_for_context = None
    user_output = None
    commit_message = None
    done = True  # Assume done unless we execute data-gathering commands

    # Execute commands
    for cmd in parse_result.commands:
        # Skip cancelled commands
        if cmd.id in cancelled_ids or cmd.command == 'CANCEL':
            continue

        # Execute based on command type
        if cmd.command == 'OUTPUT':
            user_output = cmd.args.get('text', '')
            temporary_results.append({'id': cmd.id, 'status': 'success'})

        elif cmd.command == 'NOTES':
            notes_for_context = cmd.args.get('text', '')
            temporary_results.append({'id': cmd.id, 'status': 'success'})

        elif cmd.command == 'KEEP_CONTEXT':
            keep_context = True
            temporary_results.append({'id': cmd.id, 'status': 'success'})

        elif cmd.command == 'OVERWRITE':
            result = apply_overwrite(cmd, maca.worktree_path)
            temporary_results.append(result)

        elif cmd.command == 'UPDATE':
            result = apply_single_update(cmd, maca.worktree_path)
            temporary_results.append(result)

        elif cmd.command == 'RENAME':
            result = apply_rename(cmd, maca.worktree_path)
            temporary_results.append(result)

        elif cmd.command == 'DELETE':
            result = apply_delete(cmd, maca.worktree_path)
            temporary_results.append(result)

        elif cmd.command == 'READ':
            result = execute_read(cmd, maca.worktree_path)
            temporary_results.append(result)
            done = False  # Data gathering - not done yet

        elif cmd.command == 'SEARCH':
            result = execute_search(cmd, maca.worktree_path)
            temporary_results.append(result)
            done = False

        elif cmd.command == 'SHELL':
            result = execute_shell(cmd, maca.worktree_path, maca.repo_root)
            temporary_results.append(result)
            done = False

        elif cmd.command == 'PROCESS':
            result = execute_process(cmd, maca)
            temporary_results.append(result)
            done = False

        elif cmd.command == 'ASK':
            result = execute_ask(cmd, maca)
            temporary_results.append(result)
            # Questions don't prevent completion

        elif cmd.command == 'PROPOSE_MERGE':
            commit_message = cmd.args.get('message', 'No message provided')
            temporary_results.append({'id': cmd.id, 'status': 'success'})
            # Will be handled below

        else:
            temporary_results.append({
                'id': cmd.id,
                'status': 'error',
                'error': f'Unknown command: {cmd.command}'
            })

    # Show user output if provided
    if user_output:
        cprint(C_GOOD, user_output)

    # Add notes to context if provided
    if notes_for_context:
        maca.add_message({'role': 'assistant', 'content': f"[Notes] {notes_for_context}"})

    # Check if any file modifications occurred
    if maca.last_head_commit != git_ops.get_head_commit(maca.worktree_path):
        # Determine commit description
        file_cmds = [c for c in parse_result.commands if c.command in ('OVERWRITE', 'UPDATE', 'RENAME', 'DELETE')]
        if file_cmds:
            paths = []
            for c in file_cmds:
                if 'path' in c.args:
                    paths.append(c.args['path'])
                if 'old_path' in c.args:
                    paths.append(c.args['old_path'])
            description = f"Modified {', '.join(paths[:3])}" + ('...' if len(paths) > 3 else '')
        else:
            description = "File changes"

        git_ops.commit_changes(maca.worktree_path, f"MACA: {description}")
        maca.last_head_commit = git_ops.get_head_commit(maca.worktree_path)

    # Handle merge proposal
    if commit_message and done:
        # In non-interactive mode, auto-merge
        if not maca.non_interactive:
            # Ask user for approval
            cprint(C_GOOD, '\n✓ Ready to merge!\n')
            cprint(C_INFO, f'Commit message:\n{commit_message}\n')
            response_choice = choice(
                message='Merge to main?',
                options=[
                    ('yes', 'Merge and continue'),
                    ('no', 'Continue without merging'),
                ]
            )
            if response_choice != 'yes':
                # User declined - continue session
                pass
            else:
                # Merge
                cprint(C_INFO, 'Merging changes...')
                conflict = git_ops.merge_to_main(maca.repo_root, maca.worktree_path, maca.branch_name, commit_message)

                if conflict:
                    cprint(C_BAD, "⚠ Merge conflicts!")
                    error_msg = f"Merge conflict while rebasing. Please resolve merge conflicts by reading the affected files and using UPDATE to resolve the conflicts. Then use SHELL to run `git add <filename>.. && git rebase --continue`, before trying again with another PROPOSE_MERGE. Here is the rebase output:\n\n{conflict}"
                    maca.add_message({"role": "user", "content": error_msg})
                    long_term_results = [{'id': r['id'], 'status': r['status']} for r in temporary_results]
                    return (temporary_results, long_term_results, False, parse_result.thinking)

                maca.add_message({"role": "user", "content": "Squashed and merged into main! You're now working on a fresh feature branch."})

                # Cleanup
                git_ops.cleanup_session(maca.repo_root, maca.worktree_path, maca.branch_name)
                cprint(C_GOOD, '✓ Squashed and merged!')

                # Create new worktree for next task
                maca.worktree_path, maca.branch_name = git_ops.create_session_worktree(maca.repo_root, maca.session_id)
        else:
            # Non-interactive mode - auto-merge
            cprint(C_INFO, 'Merging changes...')
            conflict = git_ops.merge_to_main(maca.repo_root, maca.worktree_path, maca.branch_name, commit_message)

            if conflict:
                cprint(C_BAD, "⚠ Merge conflicts in non-interactive mode!")
                # Return error results instead of exiting
                long_term_results = [{'id': r['id'], 'status': r['status']} for r in temporary_results]
                return (temporary_results, long_term_results, False, parse_result.thinking)

            cprint(C_GOOD, '✓ Squashed and merged!')
            # Mark as done and return - maca.run() will exit naturally in non-interactive mode
            done = True

    # Create long-term results (omit large data)
    long_term_results = []
    for result in temporary_results:
        long_term = {'id': result['id'], 'status': result['status']}

        # Copy metadata but omit large data fields
        for key, value in result.items():
            if key in ('id', 'status'):
                continue
            if key in ('data', 'output', 'matches', 'context', 'result'):
                long_term[key] = 'OMITTED'
            else:
                long_term[key] = value

        long_term_results.append(long_term)

    # Manage context cleanup
    if not keep_context:
        maca.clear_temporary_messages()

    return (temporary_results, long_term_results, done, parse_result.thinking)
