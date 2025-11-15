#!/usr/bin/env python3
"""Git operations for agentic coding assistant worktree management."""

import subprocess
import os
import re
from pathlib import Path
from typing import Optional

from utils import C_GOOD, C_INFO, C_NORMAL, cprint


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
        readme_path.write_text('# Project\n\nInitialized by maca.\n')
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


def get_head_commit(cwd='.'):
    """Get the current HEAD commit hash."""
    result = run_git('rev-parse', 'HEAD', cwd=cwd)
    return result.stdout.strip()


# def get_commits_between(old_commit, new_commit, cwd='.'):
#     """
#     Get list of commits between old_commit and new_commit.

#     Returns list of dicts with 'hash' and 'message' (first line only).
#     """
#     # Format: <hash> <first line of message>
#     result = run_git('log', '--format=%H %s', f'{old_commit}..{new_commit}', cwd=cwd, check=False)

#     if result.returncode != 0 or not result.stdout.strip():
#         return []

#     commits = []
#     for line in result.stdout.strip().split('\n'):
#         if line:
#             parts = line.split(' ', 1)
#             if len(parts) == 2:
#                 commits.append({
#                     'hash': parts[0][:8],  # Short hash
#                     'message': parts[1]
#                 })

#     return commits


# def get_changed_files_between(old_commit, new_commit, cwd='.'):
#     """
#     Get list of files changed between old_commit and new_commit.

#     Returns list of file paths.
#     """
#     result = run_git('diff', '--name-only', old_commit, new_commit, cwd=cwd, check=False)

#     if result.returncode != 0 or not result.stdout.strip():
#         return []

#     return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]


def find_next_session_id(repo_root):
    """Find the next available session ID by checking .maca directory."""
    maca_dir = Path(repo_root) / '.maca'
    maca_dir.mkdir(exist_ok=True)

    # Find all existing session directories
    existing = []
    for item in maca_dir.iterdir():
        if item.is_dir() and item.name.isdigit():
            existing.append(int(item.name))

    return max(existing, default=0) + 1


def create_session_worktree(repo_root, session_id):
    """Create a new branch and worktree for the session."""
    branch_name = f'maca/{session_id}'
    
    maca_dir = Path(repo_root) / '.maca'
    maca_dir.mkdir(parents=True, exist_ok=True)

    worktree_path = maca_dir / str(session_id)

    # Get current branch to branch from
    current_branch = get_current_branch(cwd=repo_root)

    # Clean up state worktrees and branches
    run_git('worktree', 'prune', cwd=repo_root)

    # Create new branch
    run_git('branch', '-f', branch_name, current_branch, cwd=repo_root)

    # Create worktree
    run_git('worktree', 'add', str(worktree_path), branch_name, cwd=repo_root)

    # Create .scratch directory for temporary analysis files
    scratch_dir = worktree_path / '.scratch'
    scratch_dir.mkdir(exist_ok=True)

    cprint(
        C_GOOD, f'Session {session_id} created',
        C_NORMAL, ' (branch: ', C_INFO, branch_name,
        C_NORMAL, ', worktree: ', C_INFO, str(worktree_path.relative_to(repo_root)), C_NORMAL, ')',
    )

    return worktree_path, branch_name


def commit_changes(worktree_path, message):
    """Commit all changes in the worktree with the given message, excluding .scratch and .maca."""
    # Add all changes (including untracked files), but exclude .scratch and .maca
    run_git('add', '-A', ':!.scratch', ':!.maca', cwd=worktree_path)

    # Commit
    return run_git('commit', '-m', message, cwd=worktree_path, check=False).returncode == 0


def generate_descriptive_branch_name(commit_message):
    """
    Generate a descriptive branch name from commit message.

    Returns a branch name (without maca/ prefix).
    """
    # Extract first line of commit message
    first_line = commit_message.split('\n')[0].strip()

    # Remove common prefixes
    for prefix in ['Add', 'Update', 'Fix', 'Remove', 'Refactor', 'Implement']:
        if first_line.startswith(prefix + ' '):
            first_line = first_line[len(prefix) + 1:]
            break

    # Convert to branch name format (lowercase, hyphenated, max 40 chars)
    name = first_line.lower()
    name = re.sub(r'[^a-z0-9\s-]', '', name)  # Remove special chars
    name = re.sub(r'\s+', '-', name)  # Replace spaces with hyphens
    name = re.sub(r'-+', '-', name)  # Collapse multiple hyphens
    name = name.strip('-')  # Remove leading/trailing hyphens
    name = name[:40]  # Limit length
    name = name.rstrip('-')  # Remove trailing hyphen if truncated

    # Ensure we have something
    if not name:
        name = 'changes'

    return name


def merge_to_main(root_path, worktree_path, org_branch_name, commit_message):
    """Merge the session branch into main using squash + rebase + ff strategy."""
    root_branch = get_current_branch(cwd=root_path)
    worktree_branch = get_current_branch(cwd=worktree_path)

    # Generate descriptive branch name for preserving history
    descriptive_branch = org_branch_name + '-' + generate_descriptive_branch_name(commit_message)
    run_git('checkout', '-b', descriptive_branch, cwd=worktree_path, check=False) # May err if we rerun after rebase conflict

    # Get the merge base
    base_commit = run_git('merge-base', root_branch, worktree_branch, cwd=worktree_path).stdout.strip()

    # Append preservation note to commit message
    enhanced_message = commit_message.rstrip() + f'\n\nThe original chain of MACA commits is kept in the {descriptive_branch} branch.'

    # Soft reset to base
    run_git('reset', '--soft', base_commit, cwd=worktree_path)

    # Stage all changes (excluding .scratch and .maca)
    run_git('add', '-A', ':!.scratch', ':!.maca', cwd=worktree_path)

    # Commit everything as one commit with enhanced message
    result = run_git('commit', '-m', enhanced_message, cwd=worktree_path, check=False)
    if result.returncode != 0:
        # If commit fails, it might be because there are no changes (edge case)
        # Just continue without erroring
        if 'nothing to commit' in result.stdout + result.stderr:
            return None
        else:
            # Real error, re-raise
            raise GitError(f"Git command failed: git commit\nstdout: {result.stdout}\nstderr: {result.stderr}")

    # Try to rebase the session branch onto main
    result = run_git('rebase', root_branch, cwd=worktree_path, check=False)

    if result.returncode != 0:
        return result.stdout

    # Fast-forward merge the rebased descriptive branch
    run_git('merge', '--ff-only', descriptive_branch, cwd=root_path)


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
