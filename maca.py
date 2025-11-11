#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import sys
from pathlib import Path
from typing import Optional

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.history import FileHistory

import git_ops
import context
import tools
from utils import color_print


# Module-level state
initial_prompt = None
repo_path = None
repo_root = None
session_id = None
worktree_path = None
branch_name = None
history = None


def ensure_git_repo():
    """Ensure we're in a git repository, or offer to initialize one."""
    global repo_root

    if not git_ops.is_git_repo(repo_path):
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

        git_ops.init_git_repo(repo_path)
        color_print(('ansigreen', 'Git repository initialized.'))

    repo_root = git_ops.get_repo_root(repo_path)


def create_session():
    """Create a new session with worktree and branch."""
    global session_id, worktree_path, branch_name

    # Find next session ID
    session_id = git_ops.find_next_session_id(repo_root)

    # Create worktree and branch
    worktree_path, branch_name = git_ops.create_session_worktree(repo_root, session_id)

    color_print(
        ('ansigreen', f'Session {session_id} created'),
        ' (branch: ', ('ansicyan', branch_name),
        ', worktree: ', ('ansicyan', str(worktree_path.relative_to(repo_root))), ')',
    )


def run(directory: str, task: str | None, model: str | None):
    """Run the main MACA loop."""
    global initial_prompt, repo_path, history

    initial_prompt = task

    repo_path = Path(directory).resolve()
    ensure_git_repo()

    # Setup shared input history
    history_file = repo_root / '.maca' / 'history'
    history_file.parent.mkdir(exist_ok=True)
    history = FileHistory(str(history_file))

    # Create session
    create_session()

    # Initialize context
    context.initialize(model=model or 'auto')

    # Auto-call list_files for top-level directory to give context about project structure
    try:
        top_files_result = tools.execute_tool('list_files', {'include': '*'})
        # Add as a system message so context knows what files are in the top directory
        top_files_msg = f"Top-level directory contains {top_files_result['total_count']} files"
        if top_files_result['files']:
            files_list = [f['path'] for f in top_files_result['files']]
            top_files_msg += f":\n" + "\n".join(f"- {f}" for f in files_list)
        context.add_message({'role': 'system', 'content': top_files_msg})
    except Exception as e:
        # Don't fail if this doesn't work
        pass

    # Main loop
    while True:
        # Get initial prompt if this is a new task
        prompt = initial_prompt
        if prompt:
            initial_prompt = None  # Only use command line arg for first iteration
        else:
            color_print(('ansiyellow', 'Enter your task (press Alt+Enter or Esc+Enter to submit):'))
            prompt = pt_prompt("> ", multiline=True, history=history).strip()

        if prompt:
            context.add_message({"role": "user", "content": prompt})
            context.run()
