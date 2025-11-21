#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

from copy import copy, deepcopy
import sys
import json
from pathlib import Path
from typing import Dict, Any, Optional
from unittest import result

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import choice
from prompt_toolkit.history import FileHistory

import git_ops
import tools
from command_parser import format_command_results
from utils import cprint, compute_diff, C_GOOD, C_BAD, C_NORMAL, C_IMPORTANT, C_INFO, C_LOG
from llm import call_llm, get_cumulative_cost
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


    def __init__(self, directory: str, task: str | None, model: str | None, non_interactive: bool = False, verbose: bool = False):
        """
        Initialize MACA with all required attributes.

        Args:
            directory: Project directory path
            task: Initial task description (optional)
            model: Model to use for LLM calls
            non_interactive: Run in non-interactive mode
            verbose: Enable verbose logging mode
        """
        # Basic configuration
        self.initial_prompt = task
        self.model = model or 'anthropic/claude-sonnet-4.5'
        self.non_interactive = non_interactive
        self.verbose = verbose

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
        """Update state tracking for AGENTS.md, code_map, and CHECKLIST.txt."""
        agents_md_path = self.repo_root / 'AGENTS.md'

        # Ensure .scratch directory exists
        scratch_dir = self.worktree_path / '.scratch'
        scratch_dir.mkdir(exist_ok=True)

        checklist_path = scratch_dir / 'CHECKLIST.txt'
        state = {
            "AGENTS.md": agents_md_path.read_text() if agents_md_path.exists() else "--None yet--",
            "Code Map": code_map.generate_code_map(str(self.worktree_path)),
            "CHECKLIST.txt": checklist_path.read_text() if checklist_path.exists() else "--None yet--"
        }

        if self.prev_state:
            for name, new in state.items():
                old = self.prev_state.get(name, '')
                if new != old:
                    diff = compute_diff(old, new)
                    if diff:
                        content = f"[[{name} Update]]\n\n```diff\n{diff}\n```"
                        self.state_delta_threshold -= len(content)
                        self.add_message({
                            'role': 'user',
                            'content': content
                        }, 'state')

        if not self.prev_state:
            org_size = 0
            for name, new in state.items():
                org_size += len(new)
                self.add_message({
                    'role': 'user',
                    'content': f"[[{name}]]\n\n{new}"
                }, 'state')
            
            self.state_delta_threshold = int(0.25 * org_size)

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
            self.long_term_messages = self.permanent_messages.copy()
            self.prev_state = None
            self.update_state()
        self.messages = self.long_term_messages.copy()  # Make a copy to avoid aliasing


    def run_main_loop(self):
        """
        Run the main interaction loop until completion.

        Returns:
            None (runs until complete() is called)
        """
        done = False
        last_cache_control_content = None

        # Loop until completion
        while not done:

            # The message previous to the first transient message gets the cache control header
            if last_cache_control_content:
                del last_cache_control_content['cache_control']
            for index, msg in enumerate(self.messages):
                if msg not in self.long_term_messages:
                    if index > 0:
                        last_cache_control_content = self.messages[index-1]['content']
                        if not isinstance(last_cache_control_content, dict):
                            last_cache_control_content = self.messages[index-1]['content'] = {"type": "text", "text": last_cache_control_content}
                        last_cache_control_content['cache_control'] = {'type': 'ephemeral'}
                    break

            # Call LLM (no tool schemas - just text output)
            result = call_llm(
                model=self.model,
                messages=self.messages,
                tool_schemas=None,
            )

            # Get text content from message
            message = result['message']
            text_content = message.get('content', '')

            # Log thinking text
            log(tag='llm_output', content=text_content[:500])

            # Execute commands from text
            (temporary_results, long_term_results, done, thinking) = tools.execute_commands(text_content, self)

            # Add assistant message with thinking to context
            self.add_message({
                'role': 'assistant',
                'content': thinking
            }, 'normal')

            # Format and add command results
            temp_results_text = format_command_results(temporary_results, long_term=False)
            long_results_text = format_command_results(long_term_results, long_term=True)

            # Add temporary results (with full data)
            self.add_message({
                'role': 'user',
                'content': temp_results_text
            }, 'temporary')

            # Add long-term results (with OMITTED data)
            self.add_message({
                'role': 'user',
                'content': long_results_text
            }, 'long-term-only')


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

        # Enable verbose mode if requested
        if self.verbose:
            logger.set_verbose(True)
            cprint(C_GOOD, '✓ Verbose mode enabled')

        # Load system prompt
        self._load_system_prompt()

        # Main loop
        while True:
            # Get initial prompt if this is a new task
            prompt = self.initial_prompt
            if prompt:
                self.initial_prompt = None  # Only use command line arg for first iteration
            else:
                # In non-interactive mode without a task, exit
                if self.non_interactive:
                    break
                cprint(C_IMPORTANT, 'Enter your task (press Alt+Enter or Esc+Enter to submit):')
                prompt = pt_prompt("> ", multiline=True, history=self.history).strip()

            if prompt:
                # Check for special commands
                if prompt == '/verbose on':
                    logger.set_verbose(True)
                    cprint(C_GOOD, '✓ Verbose mode enabled')
                    continue
                elif prompt == '/verbose off':
                    logger.set_verbose(False)
                    cprint(C_GOOD, '✓ Verbose mode disabled')
                    continue

                self.update_state()
                self.add_message({"role": "user", "content": prompt})
                self.run_main_loop()

                # In non-interactive mode, exit after completing the task
                if self.non_interactive:
                    break



if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='MACA - Multi-Agent Coding Assistant')
    parser.add_argument('task', nargs='?', help='Initial task description')
    parser.add_argument('-m', '--model', help='Model to use (default: anthropic/claude-sonnet-4.5)')
    parser.add_argument('-d', '--directory', default='.', help='Project directory (default: current)')
    parser.add_argument('-n', '--non-interactive', action='store_true', help='Run in non-interactive mode (requires task argument)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable verbose logging mode')
    args = parser.parse_args()

    # Validate non-interactive mode
    if args.non_interactive and not args.task:
        cprint(C_BAD, 'Error: --non-interactive (-n) requires a task argument')
        sys.exit(1)

    # Create and run MACA
    maca = MACA(
        directory=args.directory,
        task=args.task,
        model=args.model,
        non_interactive=args.non_interactive,
        verbose=args.verbose
    )
    maca.run()
