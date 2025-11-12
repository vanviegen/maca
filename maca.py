#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import sys
import os
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.history import FileHistory

import git_ops
import tools
from utils import cprint, call_llm, compute_diff, get_cumulative_cost, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO
from logger import log
import logger
import code_map


class ContextError(Exception):
    """Context operation failed."""
    pass


class MACA:
    """Main orchestration class for the coding assistant."""

    def __init__(self, directory: str, task: str | None, model: str | None, api_key: str):
        """
        Initialize MACA with all required attributes.

        Args:
            directory: Project directory path
            task: Initial task description (optional)
            model: Model to use for LLM calls
            api_key: OpenRouter API key
        """
        # Basic configuration
        self.initial_prompt = task
        self.model = model or 'anthropic/claude-sonnet-4.5'
        self.api_key = api_key

        # Repository paths
        self.repo_path = Path(directory).resolve()
        self.repo_root = None  # Set during ensure_git_repo

        # Session management
        self.session_id = None  # Set during create_session
        self.worktree_path = None  # Set during create_session
        self.branch_name = None  # Set during create_session

        # LLM context management
        self._messages = []
        self.last_head_commit = None

        # State tracking for AGENTS.md and code_map
        self.agents_md_state = None  # Current AGENTS.md content
        self.code_map_state = None  # Current code map content
        self.state_message_indices = []  # Track indices of state update messages
        self.state_delta_size = 0
        self.prev_state = None

        # History (initialized after repo_root is set)
        self.history = None

    def ensure_git_repo(self):
        """Ensure we're in a git repository, or offer to initialize one."""
        if not git_ops.is_git_repo(self.repo_path):
            cprint(C_BAD, 'Not in a git repository.')

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
            cprint(C_GOOD, 'Git repository initialized.')

        return git_ops.get_repo_root(self.repo_path)

    def create_session(self):
        """Create a new session with worktree and branch."""
        # Find next session ID
        self.session_id = git_ops.find_next_session_id(self.repo_root)

        # Create worktree and branch
        self.worktree_path, self.branch_name = git_ops.create_session_worktree(self.repo_root, self.session_id)

        cprint(
            C_GOOD, f'Session {self.session_id} created',
            C_NORMAL, ' (branch: ', C_INFO, self.branch_name,
            C_NORMAL, ', worktree: ', C_INFO, str(self.worktree_path.relative_to(self.repo_root)), C_NORMAL, ')',
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

    def update_state(self):
        """Update state tracking for AGENTS.md and code_map."""
        agents_md_path = self.repo_root / 'AGENTS.md'
        state = {
            "AGENTS.md": agents_md_path.read_text() if agents_md_path.exists() else "--None yet--",
            "Code Map": code_map.generate_code_map(str(self.worktree_path))
        }

        if self.prev_state:
            org_size = 0
            for name, new in state.items():
                org_size += len(new)
                old = self.prev_state.get(name, '')
                if new != old:
                    diff = compute_diff(old, new)
                    if diff:
                        content = f"[[{name} Update]]\n\n```diff\n{diff}\n```"
                        self.add_message({
                            'role': 'user',
                            'content': content
                        })
                        self.state_message_indices.append(len(self._messages) - 1)
                        self.state_delta_size += len(content)

            if self.state_delta_size > org_size * 0.25:
                cprint(C_IMPORTANT, '→ State changes exceed 25% of original size, rewriting history')
                # Remove all state update messages (walk backwards to preserve indices)
                for idx in reversed(self.state_message_indices):
                    if idx < len(self._messages):
                        del self._messages[idx]

                # Clear the tracking list
                self.state_message_indices.clear()
                self.state_delta_size = 0
                self.prev_state = None

        if not self.prev_state:
            for name, new in state.items():
                self.add_message({
                    'role': 'user',
                    'content': f"[[{name}]]\n\n{new}"
                })
                self.state_message_indices.append(len(self._messages) - 1)

        self.prev_state = state

    def add_message(self, message: Dict):
        """Add a message dict to the context and the log."""
        log(tag='message', **message)
        self._messages.append(message)

    def _check_state_changes(self):
        """Check if AGENTS.md or code_map changed and update state."""
        self.update_state()

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

        # Should always be 'respond' in new architecture
        if tool_name != 'respond':
            raise ContextError(f"Unexpected tool call: {tool_name} (expected 'respond')")

        # Log tool call
        log(tag='tool_call', tool=tool_name, args=str(tool_args))

        # Print tool info
        think_out_loud = tool_args.get('think_out_loud', '')
        cprint(C_GOOD, '→ ', C_IMPORTANT, f"respond: {think_out_loud}")

        # Execute tool
        tool_start = time.time()
        try:
            immediate_result, context_summary = tools.respond(
                **tool_args,
                maca=self
            )
            tool_duration = time.time() - tool_start
        except Exception as err:
            immediate_result = {"error": str(err)}
            context_summary = f"respond: error"
            tool_duration = time.time() - tool_start

        # Check if this is a completion signal
        if isinstance(immediate_result, tools.ReadyResult):
            immediate_result = immediate_result.result
            completed = True

        # Log tool result
        if completed:
            log(tag='tool_result', tool=tool_name, duration=tool_duration, result="completed", completed=True)
        elif isinstance(immediate_result, dict) and 'error' in immediate_result:
            log(tag='tool_result', tool=tool_name, duration=tool_duration, error=immediate_result['error'])
        else:
            result_preview = str(immediate_result)[:100] + ('...' if len(str(immediate_result)) > 100 else '')
            log(tag='tool_result', tool=tool_name, duration=tool_duration, result=result_preview, completed=False)

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

            cprint(C_IMPORTANT, f'→ Long output ({len(immediate_output)} chars), showing once with ephemeral cache')

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
        commit_msg = "respond"
        if git_ops.commit_changes(self.worktree_path, commit_msg):
            cprint(C_GOOD, '✓ Committed changes')

            # Check if AGENTS.md or code_map changed
            self._check_state_changes()

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
                cprint(C_GOOD, '✓ Replaced long output with summary')
                pending_summary_replacement = None

            # Print thinking message
            cprint(C_INFO, "Thinking...")

            # Call LLM (retry logic is in call_llm)
            result = call_llm(
                api_key=self.api_key,
                model=self.model,
                messages=self._messages,
                tool_schemas=list(tools.TOOL_SCHEMAS.values()),
            )

            # Add assistant message to history
            self.add_message(result['message'])

            # Process tool call from LLM response
            message = result['message']
            completed, new_pending = self.process_tool_call_from_message(message)
            if new_pending:
                pending_summary_replacement = new_pending

            if completed:
                cprint(C_GOOD, '✓ Task completed. Total cost: ', C_IMPORTANT, f'{get_cumulative_cost()}μ$')
                log(tag='complete', total_cost=get_cumulative_cost())
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
        logger.init(self.repo_root, self.session_id)

        # Load system prompt
        self._load_system_prompt()

        # Main loop
        while True:
            # Get initial prompt if this is a new task
            prompt = self.initial_prompt
            if prompt:
                self.initial_prompt = None  # Only use command line arg for first iteration
            else:
                cprint(C_IMPORTANT, 'Enter your task (press Alt+Enter or Esc+Enter to submit):')
                prompt = pt_prompt("> ", multiline=True, history=self.history).strip()

            if prompt:
                self.update_state()
                self.add_message({"role": "user", "content": prompt})
                self.run_main_loop()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='MACA - Multi-Agent Coding Assistant')
    parser.add_argument('task', nargs='?', help='Initial task description')
    parser.add_argument('-m', '--model', help='Model to use (default: anthropic/claude-sonnet-4.5)')
    parser.add_argument('-d', '--directory', default='.', help='Project directory (default: current)')
    args = parser.parse_args()

    # Get API key from environment
    api_key = os.environ.get('OPENROUTER_API_KEY')
    if not api_key:
        cprint(C_BAD, 'Error: OPENROUTER_API_KEY environment variable not set')
        sys.exit(1)

    # Create and run MACA
    maca = MACA(
        directory=args.directory,
        task=args.task,
        model=args.model,
        api_key=api_key
    )
    maca.run()
