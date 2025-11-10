#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import argparse
import sys
from pathlib import Path
from typing import Dict, Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.history import FileHistory

import git_ops
import context
import tools
from utils import color_print


class MACA:
    """Main orchestration class for the multi-agent coding assistant."""

    def __init__(self):
        parser = argparse.ArgumentParser(
            prog='maca',
            description='Multi-Agent Coding Assistant',
        )
        parser.add_argument('task', nargs='*', help='Initial task description')
        parser.add_argument('-m', '--model', default='anthropic/claude-sonnet-4.5',
                            help='Model to use for main context')
        parser.add_argument('-d', '--directory', default='.',
                            help='Project directory (default: current directory)')
        args = parser.parse_args()

        self.repo_path = Path(args.directory).resolve()
        self.initial_prompt = ' '.join(args.task) if args.task else None
        self.repo_root = None
        self.session_id = None
        self.worktree_path = None
        self.branch_name = None
        self.main_context = None
        self.subcontexts: Dict[str, context.Context] = {}
        self.context_counters: Dict[str, int] = {}  # Track counter per context type for auto-naming

        # Setup shared input history
        history_file = Path.home() / '.maca' / 'history'
        history_file.parent.mkdir(exist_ok=True)
        self.history = FileHistory(str(history_file))

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

        self.repo_root = git_ops.get_repo_root(self.repo_path)

    def create_session(self):
        """Create a new session with worktree and branch."""
        # Find next session ID
        self.session_id = git_ops.find_next_session_id(self.repo_root)

        # Create worktree and branch
        self.worktree_path, self.branch_name = git_ops.create_session_worktree(
            self.repo_root, self.session_id
        )

        # Set global state for tools
        tools.WORKTREE_PATH = str(self.worktree_path)
        tools.REPO_ROOT = str(self.repo_root)
        tools.MACA_INSTANCE = self

        color_print(
            ('ansigreen', f'Session {self.session_id} created'),
            ' (branch: ', ('ansicyan', self.branch_name),
            ', worktree: ', ('ansicyan', str(self.worktree_path.relative_to(self.repo_root))), ')',
        )

    def get_initial_prompt(self, prompt_arg: Optional[str] = None) -> str:
        """Get the initial prompt from user."""
        if prompt_arg:
            return prompt_arg

        while True:
            color_print(('ansiyellow', 'Enter your task (press Alt+Enter or Esc+Enter to submit):'))
            prompt = pt_prompt("> ", multiline=True, history=self.history).strip()
            return prompt


    def handle_completion(self, result: str) -> bool:
        """
        Handle task completion. Returns True if user approves, False otherwise.

        Args:
            result: The completion result from main context

        Returns:
            True if approved and merged, False if user wants changes
        """
        color_print(
            '\n',
            ('ansigreen', 'Task completed!'),
            f'\n{result}\n',
        )

        # Ask for approval
        response = radiolist_dialog(
            title='Task Complete',
            text='Are you satisfied with the result?',
            values=[
                ('yes', 'Yes, merge into main'),
                ('no', 'No, I want changes'),
                ('cancel', 'Cancel (keep worktree for manual review)')
            ]
        ).run()

        if response == 'yes':
            self.merge_and_cleanup()
            return True
        elif response == 'no':
            feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=self.history)
            self.main_context.add_message('user', feedback)
            return False
        else:
            print("Keeping worktree for manual review.")
            sys.exit(0)

    def merge_and_cleanup(self):
        """Merge the session branch into main and cleanup."""
        color_print(('ansicyan', 'Merging changes...'))

        # Get a commit message
        commit_msg = pt_prompt("Enter commit message for the squashed commit:\n> ", multiline=True, history=self.history)

        # Merge
        success, message = git_ops.merge_to_main(
            self.repo_root,
            self.worktree_path,
            self.branch_name,
            commit_msg
        )

        if not success:
            color_print(('ansired', f'Merge failed: {message}'))
            print("You may need to resolve conflicts manually or spawn a merge context.")
            # TODO: Spawn merge context here
            sys.exit(1)

        # Cleanup
        git_ops.cleanup_session(self.repo_root, self.worktree_path, self.branch_name)

        color_print(('ansigreen', '✓ Merged and cleaned up'))

        # Reset session for next task
        self.create_session()

    def run(self):
        """
        Main orchestration loop.

        Args:
            initial_prompt: Optional initial prompt from command line
        """
        # Ensure git repo
        self.ensure_git_repo()

        # Create session
        self.create_session()

        # Initialize main context
        self.main_context = context.Context(
            context_type='_main',
            worktree_path=self.worktree_path
        )

        # Auto-call list_files for top-level directory to give context about project structure
        try:
            top_files_result = tools.execute_tool('list_files', {'path_regex': r'^[^/\\]*$'})
            # Add as a system message so main context knows what files are in the top directory
            top_files_msg = f"Top-level directory contains {top_files_result['total_count']} files"
            if top_files_result['files']:
                top_files_msg += f":\n" + "\n".join(f"- {f}" for f in top_files_result['files'])
            self.main_context.add_message('system', top_files_msg)
        except Exception as e:
            # Don't fail if this doesn't work
            pass

        # Check if AGENTS.md exists, if not suggest creating it
        self.first_llm_call = True

        # Main loop
        while True:
            # Get initial prompt if this is a new task
            prompt = self.initial_prompt
            if prompt:
                self.initial_prompt = None  # Only use command line arg for first iteration
            else:
                prompt = self.get_initial_prompt()

            if not prompt:
                print("No task provided. Exiting.")
                break

            # On first LLM call, if no AGENTS.md exists, prepend guidance to create it
            agents_path = self.worktree_path / 'AGENTS.md'
            if self.first_llm_call and not agents_path.exists():
                guidance = ("Note: This project does not have an AGENTS.md file yet. Unless the user's "
                           "request explicitly does not benefit from understanding the codebase, or they "
                           "explicitly ask not to, your first step should usually be to create a code_analysis "
                           "subcontext to analyze the project and create an initial AGENTS.md file. This file "
                           "should be short and lean, documenting key project context, architecture, and dependencies.")
                self.main_context.add_message('user', f"{guidance}\n\nUser request: {prompt}")
            else:
                # Add to main context
                self.main_context.add_message('user', prompt)

            self.first_llm_call = False

            # Main context loop
            while True:
                # Run main context (single iteration mode)
                run_result = self.main_context.run(
                    budget=None,
                    single_iteration=True,
                )

                # Check if main context completed the task
                if run_result['completed'] and run_result['tool_name'] in ['main_complete', 'complete']:
                    # Handle completion
                    approved = self.handle_completion(run_result['tool_result'])

                    if approved:
                        # Merged successfully, start new task
                        break
                    else:
                        # User wants changes, continue main loop
                        continue

                # Main context executed a tool, continue loop
                # (Context.run already added tool result to context)



if __name__ == '__main__':
    maca = MACA()
    maca.run()
