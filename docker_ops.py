#!/usr/bin/env python3
"""Docker/Podman operations for safe command execution in containers."""

import subprocess
import tempfile
import shutil
import hashlib
from pathlib import Path
from typing import List, Dict, Optional


class ContainerError(Exception):
    """Container operation failed."""
    pass


# Detect container runtime
_CONTAINER_RUNTIME = None


def get_container_runtime():
    """Detect and return the available container runtime (podman or docker)."""
    global _CONTAINER_RUNTIME
    if _CONTAINER_RUNTIME is not None:
        return _CONTAINER_RUNTIME

    # Prefer podman for rootless execution
    if shutil.which('podman'):
        _CONTAINER_RUNTIME = 'podman'
    elif shutil.which('docker'):
        _CONTAINER_RUNTIME = 'docker'
    else:
        raise ContainerError("Neither podman nor docker found in PATH")

    return _CONTAINER_RUNTIME


# Image cache to avoid rebuilding the same image
_IMAGE_CACHE = {}


def build_image(base_image: str, docker_runs: List[str]) -> str:
    """Build a Docker image with the specified RUN commands, or return cached image ID."""
    runtime = get_container_runtime()

    # Create a cache key from base image and runs
    cache_key = hashlib.sha256(
        f"{base_image}:{':'.join(docker_runs)}".encode()
    ).hexdigest()[:12]

    if cache_key in _IMAGE_CACHE:
        return _IMAGE_CACHE[cache_key]

    # Build the Dockerfile content
    dockerfile_content = f"FROM {base_image}\n"
    for run_cmd in docker_runs:
        if run_cmd.strip():
            dockerfile_content += f"{run_cmd}\n"

    # Create a temporary directory for the build context
    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile_path = Path(tmpdir) / 'Dockerfile'
        dockerfile_path.write_text(dockerfile_content)

        # Build the image
        image_tag = f'aai-build-{cache_key}'
        cmd = [
            runtime, 'build',
            '-t', image_tag,
            '-f', str(dockerfile_path),
            tmpdir
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise ContainerError(
                f"Failed to build container image:\n{result.stderr}"
            )

    _IMAGE_CACHE[cache_key] = image_tag
    return image_tag


def truncate_output(output: str, head: int, tail: int) -> str:
    """Truncate output to keep only head and tail lines."""
    lines = output.split('\n')

    if len(lines) <= head + tail:
        return output

    stripped_count = len(lines) - head - tail
    head_lines = lines[:head]
    tail_lines = lines[-tail:] if tail > 0 else []

    truncated = '\n'.join(head_lines)
    truncated += f"\n\n... {stripped_count} more lines stripped (change head/tail to see them, or use grep to search for specific output) ...\n\n"
    truncated += '\n'.join(tail_lines)

    return truncated


def run_in_container(
    command: str,
    worktree_path: Path,
    repo_root: Path,
    docker_image: str = "debian:stable",
    docker_runs: Optional[List[str]] = None,
    head: int = 50,
    tail: int = 50
) -> Dict[str, any]:
    """
    Execute a shell command in an ephemeral container.

    Args:
        command: The shell command to execute
        worktree_path: Path to the worktree to mount
        repo_root: Path to the repository root (for .git)
        docker_image: Base Docker image to use
        docker_runs: List of RUN commands to execute when building the image
        head: Number of lines to keep from the start of output
        tail: Number of lines to keep from the end of output

    Returns:
        Dict with stdout, stderr (combined and truncated), and exit_code
    """
    runtime = get_container_runtime()

    if docker_runs is None:
        docker_runs = []

    # Build the image if we have RUN commands
    if docker_runs:
        image = build_image(docker_image, docker_runs)
    else:
        image = docker_image

    # Resolve absolute paths
    worktree_abs = worktree_path.resolve()
    repo_root_abs = repo_root.resolve()
    git_dir = repo_root_abs / '.git'

    # Build the container run command
    cmd = [
        runtime, 'run',
        '--rm',  # Remove container after exit
        '-v', f'{worktree_abs}:{worktree_abs}',  # Mount worktree at same path
        '-v', f'{git_dir}:{git_dir}:ro',  # Mount .git as read-only
        '-w', str(worktree_abs),  # Set working directory
        image,
        'sh', '-c', command
    ]

    # Execute the command
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    # Combine stdout and stderr
    combined_output = result.stdout
    if result.stderr:
        combined_output += result.stderr

    # Truncate if necessary
    truncated_output = truncate_output(combined_output, head, tail)

    return {
        'stdout': truncated_output,
        'stderr': result.stderr,
        'exit_code': result.returncode
    }
