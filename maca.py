#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import sys
import os
import time
import json
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.history import FileHistory

import git_ops
import tools
from utils import color_print
from logger import Logger


class ContextError(Exception):
    """Context operation failed."""
    pass


class MACA:
    """Main orchestration class for the coding assistant."""

    def __init__(self, directory: str, task: str | None, model: str | None):
        """
        Initialize MACA with all required attributes.

        Args:
            directory: Project directory path
            task: Initial task description (optional)
            model: Model to use for LLM calls
        """
        # Basic configuration
        self.initial_prompt = task
        self.model = model or 'anthropic/claude-sonnet-4.5'

        # Repository paths
        self.repo_path = Path(directory).resolve()
        self.repo_root = None  # Set during ensure_git_repo

        # Session management
        self.session_id = None  # Set during create_session
        self.worktree_path = None  # Set during create_session
        self.branch_name = None  # Set during create_session

        # LLM context management
        self.api_key = os.environ.get('OPENROUTER_API_KEY')
        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        self._messages = []
        self.cumulative_cost = 0
        self.last_head_commit = None
        self.tool_schemas = None  # Set after system prompt is loaded

        # Logger (initialized after session is created)
        self.logger = None

        # History (initialized after repo_root is set)
        self.history = None

    def ensure_git_repo(self):
        """Ensure we're in a git repository, or offer to initialize one."""
        if not git_ops.is_git_repo(self.repo_path):
            color_print(('ansired', 'Not in a git repository.'))

            response = radiolist_dialog(
                title='Git Repository Required',
                text='MACA requires a git repository. Initialize one now?',
                values=[
                    ('yes', 'Yes, initialize git repository'),
                    ('no', 'No, exit')
                ]
            ).run()

            if response != 'yes':
                print("Exiting.")
                sys.exit(0)

            git_ops.init_git_repo(self.repo_path)
            color_print(('ansigreen', 'Git repository initialized.'))

        return git_ops.get_repo_root(self.repo_path)

    def create_session(self):
        """Create a new session with worktree and branch."""
        # Find next session ID
        self.session_id = git_ops.find_next_session_id(self.repo_root)

        # Create worktree and branch
        self.worktree_path, self.branch_name = git_ops.create_session_worktree(self.repo_root, self.session_id)

        color_print(
            ('ansigreen', f'Session {self.session_id} created'),
            ' (branch: ', ('ansicyan', self.branch_name),
            ', worktree: ', ('ansicyan', str(self.worktree_path.relative_to(self.repo_root))), ')',
        )

    def _load_system_prompt(self):
        """Load the system prompt from prompt.md."""
        # Find prompt.md (next to the script)
        script_dir = Path(__file__).parent
        prompt_path = script_dir / 'prompt.md'

        if not prompt_path.exists():
            raise ContextError(f"System prompt not found: {prompt_path}")

        system_prompt = prompt_path.read_text()

        self.add_message({
            'role': 'system',
            'content': system_prompt
        })

    def _check_head_changes(self):
        """
        Check if HEAD has changed since last invocation.

        If changed, add a system message with commit info and changed files.
        """
        if not self.worktree_path or not self.last_head_commit:
            return

        current_head = git_ops.get_head_commit(cwd=self.worktree_path)

        if current_head != self.last_head_commit:
            # HEAD has changed, gather info
            commits = git_ops.get_commits_between(self.last_head_commit, current_head, cwd=self.worktree_path)
            changed_files = git_ops.get_changed_files_between(self.last_head_commit, current_head, cwd=self.worktree_path)

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
            Dict with 'message' and 'cost' keys
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
        if self.logger:
            self.logger.log(tag="message", **message)
        self._messages.append(message)

    def process_tool_call_from_message(self, message: Dict) -> tuple[bool, Optional[tuple]]:
        """
        Extract and execute tool call from LLM message, handle result.

        Args:
            message: LLM response message containing tool_calls

        Returns:
            Tuple of (completed, pending_summary_replacement)
            - completed: True if this was a completion signal
            - pending_summary_replacement: (tool_call_id, context_summary) if long output, None otherwise
        """
        completed = False
        pending_summary_replacement = None

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
            immediate_result, context_summary = tools.execute_tool(
                tool_name,
                tool_args,
                worktree_path=self.worktree_path,
                repo_root=self.repo_root,
                history=self.history,
                maca=self
            )
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

        # Check if output is long (>500 chars) - show once then replace with summary
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

            color_print(('ansiyellow', f'→ Long output ({len(immediate_output)} chars), showing once with ephemeral cache'))

            # Schedule replacement with summary after next LLM call
            pending_summary_replacement = (tool_call['id'], context_summary)
        else:
            # Short output or completion - add directly to context
            self.add_message({
                'type': 'function_call_output',
                'call_id': tool_call['id'],
                'output': context_summary
            })

        # Check for git changes and commit if needed
        diff_stats = git_ops.get_diff_stats(self.worktree_path)
        if diff_stats:
            # Commit changes
            commit_msg = tool_name
            git_ops.commit_changes(self.worktree_path, commit_msg)
            self.logger.log(tag='commit', message=commit_msg, diff_stats=diff_stats)
            color_print(('ansigreen', '✓ Committed changes'))

        return completed, pending_summary_replacement

    def run_main_loop(self):
        """
        Run the main interaction loop until completion.

        Returns:
            None (runs until complete() is called)
        """
        completed = False
        pending_summary_replacement = None  # Track (tool_call_id, context_summary) to replace after next LLM call

        # Loop until completion
        while not completed:
            # Replace long output with summary if needed (after LLM has seen the full data)
            if pending_summary_replacement:
                tool_call_id, context_summary = pending_summary_replacement
                # Remove the last 2 messages (ephemeral cache marker + long output)
                self._messages = self._messages[:-2]
                # Add the concise summary instead
                self.add_message({
                    'type': 'function_call_output',
                    'call_id': tool_call_id,
                    'output': context_summary
                })
                color_print(('ansigreen', f'✓ Replaced long output with summary'))
                pending_summary_replacement = None

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

            # Process tool call from LLM response
            message = result['message']
            completed, new_pending = self.process_tool_call_from_message(message)
            if new_pending:
                pending_summary_replacement = new_pending

            if completed:
                color_print(('ansigreen', f'✓ Task completed. Total cost: {self.cumulative_cost}μ$'))
                self.logger.log(tag='complete')
                break

    def run(self):
        """Main entry point that sets up and runs the assistant."""
        # Ensure git repo
        self.repo_root = self.ensure_git_repo()

        # Setup shared input history
        history_file = self.repo_root / '.maca' / 'history'
        history_file.parent.mkdir(exist_ok=True)
        self.history = FileHistory(str(history_file))

        # Create session
        self.create_session()

        # Initialize logger
        self.logger = Logger(self.repo_root, self.session_id, "main")

        # Load system prompt
        self._load_system_prompt()

        # Get all tool schemas
        self.tool_schemas = tools.get_all_tool_schemas()

        # Initialize HEAD tracking
        self.last_head_commit = git_ops.get_head_commit(cwd=self.worktree_path)

        # Auto-call list_files for top-level directory to give context about project structure
        try:
            top_files_result, _ = tools.execute_tool(
                'list_files',
                {'include': '*'},
                worktree_path=self.worktree_path,
                repo_root=self.repo_root,
                history=self.history,
                maca=self
            )
            # Add as a system message so context knows what files are in the top directory
            top_files_msg = f"Top-level directory contains {top_files_result['total_count']} files"
            if top_files_result['files']:
                files_list = [f['path'] for f in top_files_result['files']]
                top_files_msg += f":\n" + "\n".join(f"- {f}" for f in files_list)
            self.add_message({'role': 'system', 'content': top_files_msg})
        except Exception as e:
            # Don't fail if this doesn't work
            pass

        # Main loop
        while True:
            # Get initial prompt if this is a new task
            prompt = self.initial_prompt
            if prompt:
                self.initial_prompt = None  # Only use command line arg for first iteration
            else:
                color_print(('ansiyellow', 'Enter your task (press Alt+Enter or Esc+Enter to submit):'))
                prompt = pt_prompt("> ", multiline=True, history=self.history).strip()

            if prompt:
                self.add_message({"role": "user", "content": prompt})
                self.run_main_loop()
