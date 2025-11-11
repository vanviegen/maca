#!/usr/bin/env python3
"""Context class for managing LLM interactions."""

import time
from pathlib import Path
from typing import Dict, Any, Optional
import json
import os
import urllib.request


class ContextError(Exception):
    """Context operation failed."""
    pass


class Context:
    """Context for managing LLM interactions."""

    def __init__(
        self,
        model: str = "auto",
        initial_message: Optional[str] = None
    ):
        """
        Initialize a context.

        Args:
            model: Model to use for this context ("auto" to use default from prompt)
            initial_message: Optional initial user message to add to context
        """
        self.context_id = "main"
        self.api_key = os.environ.get('OPENROUTER_API_KEY')
        self._messages = []
        self.cumulative_cost = 0
        self.last_head_commit = None
        self.default_model = 'openai/gpt-5-mini'

        self.logger = Logger(maca.repo_root, maca.session_id, self.context_id)

        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        # Load system prompt and parse metadata
        self.tool_names = []
        self._load_system_prompt()
        self.tool_schemas = tools.get_tool_schemas(self.tool_names, add_rationale=False)

        # Set model (use provided or default from prompt)
        if model == "auto":
            self.model = self.default_model
        else:
            self.model = model

        # Initialize HEAD tracking if we have a worktree
        self.last_head_commit = git_ops.get_head_commit(cwd=maca.worktree_path)

        # Add initial message if provided
        if initial_message:
            self.add_message({'role': 'user', 'content': initial_message})

    def _load_system_prompt(self):
        """Load the system prompt from prompt.md and parse metadata."""
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
                self.default_model = value
            elif key == 'tools':
                self.tool_names = [name.strip() for name in value.split(',')]
            else:
                raise ContextError(f"Unknown header key in {prompt_path}: {key}")

        self.add_message({
            'role': 'system',
            'content': system_prompt
        })


    def _check_head_changes(self):
        """
        Check if HEAD has changed since last invocation.

        If changed, add a system message with commit info and changed files.
        """
        if not maca.worktree_path or not self.last_head_commit:
            return

        current_head = git_ops.get_head_commit(cwd=maca.worktree_path)

        if current_head != self.last_head_commit:
            # HEAD has changed, gather info
            commits = git_ops.get_commits_between(self.last_head_commit, current_head, cwd=maca.worktree_path)
            changed_files = git_ops.get_changed_files_between(self.last_head_commit, current_head, cwd=maca.worktree_path)

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

                self.add_message({
                    'role': 'system',
                    'content': '\n'.join(message_parts)
                })

            # Update tracking
            self.last_head_commit = current_head

    def call_llm(self) -> Dict[str, Any]:
        """
        Call the LLM and return the response.

        Returns:
            Dict with 'message' and 'tool_calls' keys
        """

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'HTTP-Referer': 'https://github.com/vanviegen/maca',
            'X-Title': 'MACA - Multi-Agent Coding Assistant'
        }

        data = {
            'model': self.model,
            'messages': self._messages,
            'tools': self.tool_schemas,
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
        self.cumulative_cost += cost

        self.logger.log(tag='llm_call', model=self.model, cost=cost, prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'], duration=time.time() - start_time)
        self.logger.log(tag='full_response', **result) # debugging only

        # Add assistant message to history
        self.add_message(message)

        return {
            'message': message,
            'cost': cost
        }
    
    def add_message(self, message: Dict):
        """Add a message dict to the context and the log."""
        self.logger.log(tag="message", **message)
        self._messages.append(message)

    def run(self):
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
            self._check_head_changes()

            # Call LLM
            for _ in range(3):  # Retry up to 3 times
                try:
                    result = self.call_llm()
                    break
                except Exception as err:
                    color_print(('ansired', f"Error during LLM call: {err}. Retrying..."))
                    self.logger.log(tag='error', error=str(err))
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
            self.logger.log(tag='tool_call', tool=tool_name, args=str(tool_args))

            # Print tool info
            color_print(('ansigreen', '→'), ' Tool: ', ('ansiyellow', f"{tool_name}({tool_args})"))

            # Execute tool
            tool_start = time.time()
            try:
                immediate_result, context_summary = tools.execute_tool(tool_name, tool_args)
                tool_duration = time.time() - tool_start
            except Exception as err:
                immediate_result = {"error": str(err)}
                context_summary = f"{tool_name}: error"
                tool_duration = time.time() - tool_start

            # Check if this is a completion signal
            if isinstance(immediate_result, tools.ReadyResult):
                immediate_result = immediate_result.result
                completed = True

            self.logger.log(tag='tool_result', tool=tool_name, duration=tool_duration, result=immediate_result, completed=completed)

            # Convert immediate result to string for LLM
            immediate_output = immediate_result if isinstance(immediate_result, str) else json.dumps(immediate_result)

            # Check if output is long (>500 chars) - needs summarization
            if not completed and len(immediate_output) > 500:
                # Add ephemeral cache control marker before the long message
                self.add_message({
                    'role': 'user',
                    'content': '',
                    'cache_control': {'type': 'ephemeral'}
                })

                # Add the full immediate result (temporary, one-time view)
                self.add_message({
                    'type': 'function_call_output',
                    'call_id': tool_call['id'],
                    'output': immediate_output
                })

                color_print(('ansiyellow', f'→ Long output ({len(immediate_output)} chars), requesting summary...'))

                # Make LLM call that REQUIRES summarize_and_update_files
                summary_schemas = tools.get_tool_schemas(['summarize_and_update_files'], add_rationale=False)

                headers = {
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {self.api_key}',
                    'HTTP-Referer': 'https://github.com/vanviegen/maca',
                    'X-Title': 'MACA - Coding Assistant'
                }

                data = {
                    'model': self.model,
                    'messages': self._messages,
                    'tools': summary_schemas,
                    'usage': {"include": True},
                    'tool_choice': {'type': 'function', 'function': {'name': 'summarize_and_update_files'}},
                }

                try:
                    req = urllib.request.Request(
                        "https://openrouter.ai/api/v1/chat/completions",
                        data=json.dumps(data).encode('utf-8'),
                        headers=headers
                    )
                    with urllib.request.urlopen(req) as response:
                        summary_result = json.loads(response.read().decode('utf-8'))

                    # Extract summary tool call
                    choice = summary_result['choices'][0]
                    summary_message = choice['message']
                    usage = summary_result.get('usage', {})
                    summary_cost = int(usage.get('cost', 0) * 1_000_000)
                    self.cumulative_cost += summary_cost

                    self.logger.log(tag='llm_call', model=self.model, cost=summary_cost,
                                  prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'])

                    # Add assistant message
                    self.add_message(summary_message)

                    # Execute the summarize_and_update_files tool
                    summary_tool_calls = summary_message.get('tool_calls', [])
                    if summary_tool_calls:
                        summary_tool_call = summary_tool_calls[0]
                        summary_tool_args = json.loads(summary_tool_call['function']['arguments'])

                        # Execute summarize_and_update_files
                        _, final_summary = tools.execute_tool('summarize_and_update_files', summary_tool_args)

                        # Remove the last 3 messages (cache control, long output, summary assistant message)
                        self._messages = self._messages[:-3]

                        # Add the final summary to permanent context
                        self.add_message({
                            'type': 'function_call_output',
                            'call_id': tool_call['id'],
                            'output': final_summary
                        })

                        color_print(('ansigreen', f'✓ Summarized to: {final_summary}'))
                    else:
                        # Fallback if no tool call
                        self._messages = self._messages[:-2]
                        self.add_message({
                            'type': 'function_call_output',
                            'call_id': tool_call['id'],
                            'output': context_summary
                        })

                except Exception as e:
                    color_print(('ansired', f'Error during summarization: {e}'))
                    # Fallback to context summary
                    self._messages = self._messages[:-2]
                    self.add_message({
                        'type': 'function_call_output',
                        'call_id': tool_call['id'],
                        'output': context_summary
                    })
            else:
                # Short output or completion - use context summary
                self.add_message({
                    'type': 'function_call_output',
                    'call_id': tool_call['id'],
                    'output': context_summary
                })

            # Check for git changes and commit if needed
            diff_stats = git_ops.get_diff_stats(maca.worktree_path)
            if diff_stats:
                # Commit changes
                commit_msg = tool_name
                git_ops.commit_changes(maca.worktree_path, commit_msg)
                self.logger.log(tag='commit', message=commit_msg, diff_stats=diff_stats)
                color_print(('ansigreen', '✓ Committed changes'))

            if completed:
                color_print(('ansigreen', f'✓ Task completed. Total cost: {self.cumulative_cost}μ$'))
                self.logger.log(tag='complete')
                break



from logger import Logger
from maca import maca
from utils import color_print
import tools
import git_ops
