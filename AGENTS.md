# MACA Agent Configuration

This file contains the default configuration for the multi-agent coding assistant.

## Default Docker Configuration

The following configuration is used when executing shell commands through the `shell` tool, unless overridden by the LLM.

### Base Image
```
debian:stable
```

### Default RUN Commands
```dockerfile
RUN apt-get update && apt-get install -y build-essential git python3
```

This provides:
- **build-essential**: GCC, G++, make, and other compilation tools
- **git**: Version control operations
- **python3**: Python runtime for scripts

## Customizing for Your Project

Subcontexts can override these defaults in their `shell` tool calls by specifying:
- `docker_image`: Alternative base image
- `docker_runs`: Additional RUN commands to install project-specific dependencies

### Example: Adding Node.js
```python
shell(
    command="npm install",
    docker_runs=[
        "RUN apt-get update && apt-get install -y build-essential git python3",
        "RUN apt-get install -y nodejs npm"
    ]
)
```

### Example: Using a Different Base Image
```python
shell(
    command="python manage.py test",
    docker_image="python:3.11-slim",
    docker_runs=[
        "RUN apt-get update && apt-get install -y git",
        "RUN pip install django"
    ]
)
```

## Image Caching

MACA automatically caches built images based on the combination of `docker_image` and `docker_runs`. When using the same configuration repeatedly, builds will be fast as the cached image is reused.

## Rootless Execution

If `podman` is available on the system, MACA will prefer it over Docker for rootless container execution. This ensures file permissions in the worktree are preserved correctly.

## Volume Mounts

All shell commands execute with the following volumes mounted:
- **Worktree**: Mounted at the same path as on the host
- **.git directory**: Mounted read-only for git operations

The working directory inside the container is set to the worktree path.
