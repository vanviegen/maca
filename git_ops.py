#!/usr/bin/env python3
"""Git operations for agentic coding assistant worktree management."""

import subprocess
import os
import re
from pathlib import Path
from typing import Optional


class GitError(Exception):
    """Git operation failed."""
    pass


def run_git(*args, cwd=None, check=True, capture_output=True):
    """Run a git command and return the result."""
    cmd = ['git'] + list(args)
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        check=False
    )
    if check and result.returncode != 0:
        raise GitError(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def is_git_repo(path='.'):
    """Check if the given path is inside a git repository."""
    result = run_git('rev-parse', '--git-dir', cwd=path, check=False)
    return result.returncode == 0


def init_git_repo(path='.'):
    """Initialize a new git repository."""
    run_git('init', cwd=path)
    # Create an initial commit to have a main branch
    readme_path = Path(path) / 'README.md'
    if not readme_path.exists():
        readme_path.write_text('# Project\n\nInitialized by aai.\n')
    run_git('add', 'README.md', cwd=path)
    run_git('commit', '-m', 'Initial commit', cwd=path)


def get_repo_root(path='.'):
    """Get the root directory of the git repository."""
    result = run_git('rev-parse', '--show-toplevel', cwd=path)
    return Path(result.stdout.strip())


def get_current_branch(cwd='.'):
    """Get the name of the current branch."""
    result = run_git('rev-parse', '--abbrev-ref', 'HEAD', cwd=cwd)
    return result.stdout.strip()


def find_next_session_id(repo_root):
    """Find the next available session ID by checking .aai directory."""
    aai_dir = Path(repo_root) / '.aai'
    aai_dir.mkdir(exist_ok=True)

    # Find all existing session directories and logs
    existing = []
    for item in aai_dir.iterdir():
        if item.is_dir() and item.name.isdigit():
            existing.append(int(item.name))
        elif item.name.endswith('.log'):
            match = re.match(r'(\d+)\.log$', item.name)
            if match:
                existing.append(int(match.group(1)))

    return max(existing, default=0) + 1


def create_session_worktree(repo_root, session_id):
    """Create a new branch and worktree for the session."""
    branch_name = f'aai-{session_id}'
    worktree_path = Path(repo_root) / '.aai' / str(session_id)

    # Get current branch to branch from
    current_branch = get_current_branch(cwd=repo_root)

    # Create new branch
    run_git('branch', branch_name, current_branch, cwd=repo_root)

    # Create worktree
    run_git('worktree', 'add', str(worktree_path), branch_name, cwd=repo_root)

    return worktree_path, branch_name


def commit_changes(worktree_path, message):
    """Commit all changes in the worktree with the given message."""
    # Add all changes (including untracked files)
    run_git('add', '-A', cwd=worktree_path)

    # Check if there are changes to commit
    result = run_git('diff', '--cached', '--quiet', cwd=worktree_path, check=False)
    if result.returncode == 0:
        # No changes to commit
        return False

    # Commit
    run_git('commit', '-m', message, cwd=worktree_path)
    return True


def get_diff_stats(worktree_path):
    """Get the diff statistics for uncommitted changes."""
    # Check both staged and unstaged changes
    result = run_git('diff', '--numstat', 'HEAD', cwd=worktree_path, check=False)
    return result.stdout.strip()


def squash_commits(repo_root, branch_name):
    """Squash all commits on the branch into one commit."""
    # Get the base commit (where we branched from)
    # This is the merge-base between branch and main
    current_branch = get_current_branch(cwd=repo_root)
    result = run_git('merge-base', current_branch, branch_name, cwd=repo_root)
    base_commit = result.stdout.strip()

    # Get all commit messages on the branch (excluding auto-commits with rationale)
    result = run_git('log', '--format=%B', f'{base_commit}..{branch_name}', cwd=repo_root)
    all_messages = result.stdout.strip()

    # Soft reset to base commit
    run_git('reset', '--soft', base_commit, cwd=repo_root)

    return all_messages


def merge_to_main(repo_root, worktree_path, branch_name, commit_message):
    """Merge the session branch into main using squash + rebase + ff strategy."""
    main_branch = get_current_branch(cwd=repo_root)

    # First, squash all commits in the worktree
    # We do this by checking out the branch and doing a soft reset
    original_branch = get_current_branch(cwd=repo_root)

    # Switch to the session branch in the main repo
    run_git('checkout', branch_name, cwd=repo_root)

    # Get the merge base
    result = run_git('merge-base', main_branch, branch_name, cwd=repo_root)
    base_commit = result.stdout.strip()

    # Soft reset to base
    run_git('reset', '--soft', base_commit, cwd=repo_root)

    # Commit everything as one commit
    run_git('commit', '-m', commit_message, cwd=repo_root, check=False)

    # Go back to main branch
    run_git('checkout', main_branch, cwd=repo_root)

    # Try to rebase the session branch onto main
    result = run_git('rebase', main_branch, branch_name, cwd=repo_root, check=False)

    if result.returncode != 0:
        # Rebase failed, likely due to conflicts
        # Abort the rebase
        run_git('rebase', '--abort', cwd=repo_root, check=False)
        return False, "Merge conflicts detected"

    # Fast-forward merge
    run_git('merge', '--ff-only', branch_name, cwd=repo_root)

    return True, "Merged successfully"


def cleanup_session(repo_root, worktree_path, branch_name):
    """Clean up the worktree and branch after merge."""
    # Remove worktree
    run_git('worktree', 'remove', str(worktree_path), cwd=repo_root, check=False)

    # Delete branch
    run_git('branch', '-D', branch_name, cwd=repo_root, check=False)


def reset_worktree_to_main(repo_root, worktree_path, branch_name):
    """Reset the worktree and branch to match main (for reuse)."""
    main_branch = get_current_branch(cwd=repo_root)

    # In the worktree, reset hard to main
    run_git('fetch', 'origin', main_branch, cwd=worktree_path, check=False)
    run_git('reset', '--hard', main_branch, cwd=worktree_path)

    # Clean untracked files
    run_git('clean', '-fd', cwd=worktree_path)
