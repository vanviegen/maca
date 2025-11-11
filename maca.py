#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import json
import sys
from pathlib import Path
from typing import Dict, Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.history import FileHistory


class MACA:
    """Main orchestration class for the multi-agent coding assistant."""

    def __init__(self):
        self.initial_prompt = None
        self.repo_path = None
        self.initial_prompt = None
        self.repo_root = None
        self.session_id = None
        self.worktree_path = None
        self.branch_name = None
        self.main_context = None
        self.subcontexts: Dict[str, context.Context] = {}
        self.context_counters: Dict[str, int] = {}  # Track counter per context type for auto-naming

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

    def run(self, directory: str, task: str | None, model: str | None):
        self.initial_prompt = task
        
        self.repo_path = Path(directory).resolve()
        self.repo_root = self.ensure_git_repo()

        # Setup shared input history
        history_file = self.repo_root / '.maca' / 'history'
        history_file.parent.mkdir(exist_ok=True)
        self.history = FileHistory(str(history_file))

        # Create session
        self.create_session()

        # Initialize main context
        self.main_context = context.Context(context_type='_main', context_id='main', model=model or 'auto')

        # Auto-call list_files for top-level directory to give context about project structure
        try:
            top_files_result = tools.execute_tool('list_files', {})
            # Add as a system message so main context knows what files are in the top directory
            top_files_msg = f"Result for list_files tool with default arguments: {json.dumps(top_files_result)}"
            self.main_context.add_message({'role': 'system', 'content': top_files_msg})
        except Exception as e:
            color_print(('ansired', f"Warning: Failed to list top-level files: {e}"))

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
                self.main_context.add_message({"role": "user", "content": prompt})
                self.main_context.run()

maca = MACA()

import git_ops
import context
import tools
from utils import color_print
