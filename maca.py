#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

from copy import copy, deepcopy
import sys
import os
import time
import json
from pathlib import Path
from typing import Dict, Any, Optional
from unittest import result

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.history import FileHistory

import git_ops
import tools
from utils import cprint, call_llm, compute_diff, get_cumulative_cost, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO
from logger import log
import logger
import code_map

PERSIST_TEMPORARY = 1
PERSIST_LONG_TERM = 2 
PERSIST_PERMANENT = 4

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
        self.messages: list[Dict] = []
        self.long_term_messages: list[Dict] = []
        self.permanent_messages: list[Dict] = []
        self.last_head_commit = None

        # State tracking for AGENTS.md and code_map
        self.agents_md_state = None  # Current AGENTS.md content
        self.code_map_state = None  # Current code map content
        self.state_delta_threshold = 0
        self.prev_state = None

        # History (initialized after repo_root is set)
        self.history = None

    def ensure_git_repo(self):
        """Ensure we're in a git repository, or offer to initialize one."""
        if not git_ops.is_git_repo(self.repo_path):
            cprint(C_BAD, 'Not in a git repository.')

            response = choice(
                message='MACA requires a git repository. Initialize one now?',
                options=[
                    ('yes', 'Yes, initialize git repository'),
                    ('no', 'No, exit')
                ]
            )

            if response != 'yes':
                print("Exiting.")
                sys.exit(0)

            git_ops.init_git_repo(self.repo_path)
            cprint(C_GOOD, 'Git repository initialized.')

        return git_ops.get_repo_root(self.repo_path)

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
                        }, 'state')
            
            self.state_delta_threshold = int(0.25 * org_size)

        if not self.prev_state:
            for name, new in state.items():
                self.add_message({
                    'role': 'user',
                    'content': f"[[{name}]]\n\n{new}"
                }, 'state')

        self.prev_state = state

    def add_message(self, message: Dict, persistence = 'normal'):
        """Add a message dict to the context and the log. Persistence can be normal, temporary, long-term-only, state."""
        log(tag='message', persistence=persistence, **message)
        if persistence != 'long-term-only':
            self.messages.append(message)
        if persistence != 'temporary':
            self.long_term_messages.append(message)
            if persistence == 'state':
                self.state_delta_threshold -= len(json.dumps(message))
            else:
                self.permanent_messages.append(message)

    def clear_temporary_messages(self):
        """Clear all temporary messages from the context."""
        if self.state_delta_threshold <= 0:
            cprint(C_IMPORTANT, '→ State changes exceed 25% of original size, rewriting history')
            self.long_term_messages = self.permanent_messages
            self.prev_state = None
            self.update_state()
        self.messages = self.long_term_messages

    def run_main_loop(self):
        """
        Run the main interaction loop until completion.

        Returns:
            None (runs until complete() is called)
        """
        done = False

        # Loop until completion
        while not done:

            # Print thinking message
            cprint(C_INFO, "Thinking...")

            # Call LLM (retry logic is in call_llm)
            result = call_llm(
                api_key=self.api_key,
                model=self.model,
                messages=self.messages,
                tool_schemas=[tools.RESPOND_TOOL_SCHEMA],
            )

            # Log the full message temporarily. The respond function will strip 'message' of details,
            # so we can log the short version to long-term below.
            message = result['message']
            self.add_message(deepcopy(message), 'temporary')

            # Process tool call from LLM response
            tool_calls = message.get('tool_calls', [])
            if len(tool_calls) != 1:
                raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")
            args = json.loads(message['tool_calls'][0]['function']['arguments'])
            log(tag='tool_call', **args)
            (long_term_response, temporary_response, done) = tools.respond(**args, maca=self)

            # Serialize responses to JSON
            long_term_json = json.dumps(long_term_response, indent=2)
            temporary_json = json.dumps(temporary_response, indent=2)

            # Add assistant message (and trimmed version) to history
            self.add_message(message, 'long-term-only')

            # Add tool result messages (temporary and long-term summary)
            self.add_message({'role': 'user',
                'content': [
                    {
                        'type': 'tool_result',
                        'tool_use_id': tool_calls[0]['id'],
                        'content': temporary_json,
                        'cache_control': {'type': 'ephemeral'}
                    }
                ]
            }, 'temporary')

            self.add_message({'role': 'user',
                'content': [
                    {
                        'type': 'tool_result',
                        'tool_use_id': tool_calls[0]['id'],
                        'content': long_term_json,
                        'cache_control': {'type': 'ephemeral'}
                    }
                ]
            }, 'long-term-only')

            if done and not self.handle_done():
                done = False

        cprint(C_GOOD, '✓ Task completed. Total cost: ', C_IMPORTANT, f'{get_cumulative_cost()}μ$')
        log(tag='complete', total_cost=get_cumulative_cost())

    def handle_done(self):
        cprint(C_GOOD, '\n✓ Task completed!\n')

        # Ask for approval
        response = choice(
            message=f'How to proceed? [{self.worktree_path}]',
            options=[
                ('merge', 'Merge into main'),
                ('continue', 'Ask for further changes'),
                ('cancel', 'Leave as-is for manual review'),
                ('delete', 'Delete everything'),
            ]
        )

        if response == 'merge':
            cprint(C_INFO, 'Merging changes...')

            # Use session ID as commit message
            commit_msg = f"Session {self.session_id}"

            # Merge
            conflict = git_ops.merge_to_main(self.repo_root, self.worktree_path, self.branch_name, commit_msg)

            if conflict:
                cprint(C_BAD, "⚠ Merge conflicts!")
                error_response = {
                    "error": f"Merge conflict while rebasing. Please resolve merge conflicts by reading the affected files and using file_updates to resolve the conflicts. Then use a shell_command to run `git add <filename>.. && git rebase --continue`, before calling respond again with done=true to try the merge again. Here is the rebase output:\n\n{conflict}"
                }
                # Add error as user message so the assistant can fix it
                self.add_message({"role": "user", "content": json.dumps(error_response, indent=2)})
                return False

            # Cleanup
            git_ops.cleanup_session(self.repo_root, self.worktree_path, self.branch_name)
            cprint(C_GOOD, '✓ Merged and cleaned up')
            git_ops.create_session_worktree(self.repo_root, self.session_id)

            return True

        if response == 'continue':
            feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=self.history)
            self.add_message({"role": "user", "content": feedback})
            return False

        if response == 'delete':
            git_ops.cleanup_session(self.repo_root, self.worktree_path, self.branch_name)
            cprint(C_BAD, '✓ Deleted worktree and branch')
            return True

        cprint(C_IMPORTANT, "Keeping worktree for manual review.")
        return True


    def run(self):
        """Main entry point that sets up and runs the assistant."""
        # Ensure git repo
        self.repo_root = self.ensure_git_repo()

        # Setup shared input history
        history_file = self.repo_root / '.maca' / 'history'
        history_file.parent.mkdir(exist_ok=True)
        self.history = FileHistory(str(history_file))

        # Create session
        self.session_id = git_ops.find_next_session_id(self.repo_root)
        self.worktree_path, self.branch_name = git_ops.create_session_worktree(self.repo_root, self.session_id)

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
