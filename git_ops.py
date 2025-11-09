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


def get_commits_between(old_commit, new_commit, cwd='.'):
    """
    Get list of commits between old_commit and new_commit.

    Returns list of dicts with 'hash' and 'message' (first line only).
    """
    # Format: <hash> <first line of message>
    result = run_git('log', '--format=%H %s', f'{old_commit}..{new_commit}', cwd=cwd, check=False)

    if result.returncode != 0 or not result.stdout.strip():
        return []

    commits = []
    for line in result.stdout.strip().split('\n'):
        if line:
            parts = line.split(' ', 1)
            if len(parts) == 2:
                commits.append({
                    'hash': parts[0][:8],  # Short hash
                    'message': parts[1]
                })

    return commits


def get_changed_files_between(old_commit, new_commit, cwd='.'):
    """
    Get list of files changed between old_commit and new_commit.

    Returns list of file paths.
    """
    result = run_git('diff', '--name-only', old_commit, new_commit, cwd=cwd, check=False)

    if result.returncode != 0 or not result.stdout.strip():
        return []

    return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]


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
    branch_name = f'maca-{session_id}'
    session_dir = Path(repo_root) / '.maca' / str(session_id)
    worktree_path = session_dir / '<tree>'

    # Ensure session directory exists
    session_dir.mkdir(parents=True, exist_ok=True)

    # Get current branch to branch from
    current_branch = get_current_branch(cwd=repo_root)

    # Create new branch
    run_git('branch', branch_name, current_branch, cwd=repo_root)

    # Create worktree
    run_git('worktree', 'add', str(worktree_path), branch_name, cwd=repo_root)

    # Create .scratch directory for temporary analysis files
    scratch_dir = worktree_path / '.scratch'
    scratch_dir.mkdir(exist_ok=True)

    return worktree_path, branch_name


def commit_changes(worktree_path, message):
    """Commit all changes in the worktree with the given message, excluding .scratch and .maca."""
    # Add all changes (including untracked files), but exclude .scratch and .maca
    run_git('add', '-A', ':!.scratch', ':!.maca', cwd=worktree_path)

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


def generate_descriptive_branch_name(commit_message, repo_root):
    """
    Generate a descriptive branch name from commit message.

    Returns a branch name that doesn't conflict with existing maca/* branches.
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

    # Check if maca/<name> exists, if so add suffix
    base_name = name
    counter = 2
    while True:
        full_branch = f'maca/{name}'
        result = run_git('rev-parse', '--verify', full_branch, cwd=repo_root, check=False)
        if result.returncode != 0:
            # Branch doesn't exist, we can use it
            break
        # Branch exists, try next variant
        name = f'{base_name}-{counter}'
        counter += 1

    return name


def merge_to_main(repo_root, worktree_path, branch_name, commit_message):
    """Merge the session branch into main using squash + rebase + ff strategy."""
    main_branch = get_current_branch(cwd=repo_root)

    # First, squash all commits in the worktree
    # We do this by checking out the branch and doing a soft reset
    original_branch = get_current_branch(cwd=repo_root)

    # Switch to the session branch in the main repo
    run_git('checkout', branch_name, cwd=repo_root)

    # Save the current HEAD commit hash (before squashing)
    result = run_git('rev-parse', 'HEAD', cwd=repo_root)
    original_head = result.stdout.strip()

    # Get the merge base
    result = run_git('merge-base', main_branch, branch_name, cwd=repo_root)
    base_commit = result.stdout.strip()

    # Generate descriptive branch name for preserving history
    descriptive_name = generate_descriptive_branch_name(commit_message, repo_root)

    # Append preservation note to commit message
    enhanced_message = commit_message
    if not enhanced_message.endswith('\n'):
        enhanced_message += '\n'
    enhanced_message += f'\nThe original chain of MACA commits is kept in the maca/{descriptive_name} branch.'

    # Soft reset to base
    run_git('reset', '--soft', base_commit, cwd=repo_root)

    # Commit everything as one commit with enhanced message
    run_git('commit', '-m', enhanced_message, cwd=repo_root, check=False)

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

    # Create the descriptive branch pointing at original HEAD
    run_git('branch', f'maca/{descriptive_name}', original_head, cwd=repo_root)

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
