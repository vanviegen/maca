#!/usr/bin/env python3
"""Context management for LLM interactions."""

import time
from pathlib import Path
from typing import Dict, Any, Optional
import json
import os
import urllib.request

from logger import Logger
import maca
from utils import color_print
import tools
import git_ops


class ContextError(Exception):
    """Context operation failed."""
    pass


# Module-level state
context_id = "main"
api_key = None
_messages = []
cumulative_cost = 0
last_head_commit = None
default_model = 'openai/gpt-5-mini'
logger = None
tool_names = []
tool_schemas = []
model = None


def initialize(model_name: str = "auto", initial_message: Optional[str] = None):
    """
    Initialize the context.

    Args:
        model_name: Model to use for this context ("auto" to use default from prompt)
        initial_message: Optional initial user message to add to context
    """
    global api_key, _messages, cumulative_cost, last_head_commit, logger, tool_schemas, model

    api_key = os.environ.get('OPENROUTER_API_KEY')
    _messages = []
    cumulative_cost = 0
    last_head_commit = None

    logger = Logger(maca.repo_root, maca.session_id, context_id)

    if not api_key:
        raise ContextError("OPENROUTER_API_KEY not set")

    # Load system prompt and parse metadata
    _load_system_prompt()
    tool_schemas = tools.get_tool_schemas(tool_names, add_rationale=False)

    # Set model (use provided or default from prompt)
    if model_name == "auto":
        model = default_model
    else:
        model = model_name

    # Initialize HEAD tracking if we have a worktree
    last_head_commit = git_ops.get_head_commit(cwd=maca.worktree_path)

    # Add initial message if provided
    if initial_message:
        add_message({'role': 'user', 'content': initial_message})


def _load_system_prompt():
    """Load the system prompt from prompt.md and parse metadata."""
    global tool_names, default_model

    # Find prompt.md (next to the script)
    script_dir = Path(__file__).parent
    prompt_path = script_dir / 'prompt.md'

    if not prompt_path.exists():
        raise ContextError(f"System prompt not found: {prompt_path}")

    prompt_content = prompt_path.read_text()

    # Split into headers and prompt body on first blank line
    parts = prompt_content.split('\n\n', 1)
    if len(parts) != 2:
        raise ContextError(f"Prompt file {prompt_path} must have headers separated by blank line")

    headers_text, system_prompt = parts

    # Parse headers
    for line in headers_text.split('\n'):
        line = line.strip()
        if not line:
            continue

        if ':' not in line:
            raise ContextError(f"Invalid header format in {prompt_path}: {line}")

        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()

        if key == 'default_model':
            default_model = value
        elif key == 'tools':
            tool_names = [name.strip() for name in value.split(',')]
        else:
            raise ContextError(f"Unknown header key in {prompt_path}: {key}")

    add_message({
        'role': 'system',
        'content': system_prompt
    })


def _check_head_changes():
    """
    Check if HEAD has changed since last invocation.

    If changed, add a system message with commit info and changed files.
    """
    global last_head_commit

    if not maca.worktree_path or not last_head_commit:
        return

    current_head = git_ops.get_head_commit(cwd=maca.worktree_path)

    if current_head != last_head_commit:
        # HEAD has changed, gather info
        commits = git_ops.get_commits_between(last_head_commit, current_head, cwd=maca.worktree_path)
        changed_files = git_ops.get_changed_files_between(last_head_commit, current_head, cwd=maca.worktree_path)

        if commits or changed_files:
            # Build system message
            message_parts = ["# Repository Updates\n\nThe following changes have been made since you were last invoked:\n"]

            if commits:
                message_parts.append("\n## New Commits\n")
                for commit in commits:
                    message_parts.append(f"- `{commit['hash']}` {commit['message']}")

            if changed_files:
                message_parts.append("\n\n## Changed Files\n")
                for filepath in changed_files:
                    message_parts.append(f"- {filepath}")

            add_message({
                'role': 'system',
                'content': '\n'.join(message_parts)
            })

        # Update tracking
        last_head_commit = current_head


def call_llm() -> Dict[str, Any]:
    """
    Call the LLM and return the response.

    Returns:
        Dict with 'message' and 'tool_calls' keys
    """
    global cumulative_cost

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'HTTP-Referer': 'https://github.com/vanviegen/maca',
        'X-Title': 'MACA - Multi-Agent Coding Assistant'
    }

    data = {
        'model': model,
        'messages': _messages,
        'tools': tool_schemas,
        'usage': {"include": True},
        'tool_choice': 'required',  # Force tool use
    }
    start_time = time.time()

    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(data).encode('utf-8'),
            headers=headers
        )
        with urllib.request.urlopen(req) as response:
            result = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        if hasattr(e, 'read'):
            error_body = e.read().decode('utf-8')
            raise ContextError(f"LLM API error: {error_body}")
        raise ContextError(f"LLM API error: {str(e)}")

    # Extract response
    choice = result['choices'][0]
    message = choice['message']

    # Extract usage
    usage = result.get('usage', {})
    cost = int(usage.get('cost', 0) * 1_000_000)  # Convert dollars to microdollars
    cumulative_cost += cost

    logger.log(tag='llm_call', model=model, cost=cost, prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'], duration=time.time() - start_time)
    logger.log(tag='full_response', **result) # debugging only

    # Add assistant message to history
    add_message(message)

    return {
        'message': message,
        'cost': cost
    }


def add_message(message: Dict):
    """Add a message dict to the context and the log."""
    logger.log(tag="message", **message)
    _messages.append(message)


def run():
    """
    Run this context until completion.

    Returns:
        None (runs until complete() is called)
    """
    completed = False

    # Loop until completion
    while not completed:
        # Print thinking message
        color_print(('ansicyan', "Thinking..."))

        # Check for HEAD changes before calling LLM
        _check_head_changes()

        # Call LLM
        for _ in range(3):  # Retry up to 3 times
            try:
                result = call_llm()
                break
            except Exception as err:
                color_print(('ansired', f"Error during LLM call: {err}. Retrying..."))
                logger.log(tag='error', error=str(err))
        else:
            break

        # Extract tool calls
        message = result['message']
        cost = result['cost']

        # Extract tool info
        tool_calls = message.get('tool_calls', [])
        if len(tool_calls) != 1:
            raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")
        tool_call = tool_calls[0]
        tool_name = tool_call['function']['name']
        tool_args = json.loads(tool_call['function']['arguments'])

        # Log tool call
        logger.log(tag='tool_call', tool=tool_name, args=str(tool_args))

        # Print tool info
        color_print(('ansigreen', '→'), ' Tool: ', ('ansiyellow', f"{tool_name}({tool_args})"))

        # Execute tool
        tool_start = time.time()
        try:
            tool_result = tools.execute_tool(tool_name, tool_args)
            tool_duration = time.time() - tool_start
        except Exception as err:
            tool_result = {"error": str(err)}
            tool_duration = time.time() - tool_start

        if isinstance(tool_result, tools.ReadyResult):
            tool_result = tool_result.result
            completed = True

        logger.log(tag='tool_result', tool=tool_name, duration=tool_duration, result=tool_result, completed=completed)

        add_message({
            'type': 'function_call_output',
            'call_id': tool_call['id'],
            'output': tool_result if isinstance(tool_result, str) else json.dumps(tool_result)
        })

        # Check for git changes and commit if needed
        diff_stats = git_ops.get_diff_stats(maca.worktree_path)
        if diff_stats:
            # Commit changes
            commit_msg = tool_name
            git_ops.commit_changes(maca.worktree_path, commit_msg)
            logger.log(tag='commit', message=commit_msg, diff_stats=diff_stats)
            color_print(('ansigreen', '✓ Committed changes'))

        if completed:
            color_print(('ansigreen', f'✓ Task completed. Total cost: {cumulative_cost}μ$'))
            logger.log(tag='complete')
            break
