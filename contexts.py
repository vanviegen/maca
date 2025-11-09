#!/usr/bin/env python3
"""Context classes for managing different types of LLM interactions."""

import json
import os
import urllib.request
import difflib
from pathlib import Path
from typing import Dict, List, Any, Optional

import tools
from git_ops import get_head_commit, get_commits_between, get_changed_files_between


class ContextError(Exception):
    """Context operation failed."""
    pass


class BaseContext:
    """Base class for all contexts."""

    def __init__(
        self,
        context_id: str,
        context_type: str,
        model: str,
        api_key: Optional[str] = None,
        worktree_path: Optional[Path] = None
    ):
        """
        Initialize a context.

        Args:
            context_id: Unique identifier for this context
            context_type: Type of context (main, code_analysis, research, etc.)
            model: Model to use for this context
            api_key: OpenRouter API key (defaults to env var)
            worktree_path: Path to the worktree (for loading AGENTS.md)
        """
        self.context_id = context_id
        self.context_type = context_type
        self.model = model
        self.api_key = api_key or os.environ.get('OPENROUTER_API_KEY')
        self.worktree_path = worktree_path
        self.messages = []
        self.last_usage = {}
        self.agents_md_content = None
        self.last_head_commit = None

        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        # Load system prompt
        self._load_system_prompt()

        # Load AGENTS.md if it exists
        self._load_agents_md()

        # Add unique name info for subcontexts (main context doesn't need this)
        if self.context_type != 'main':
            self.messages.append({
                'role': 'system',
                'content': f"# Your Context Info\n\nYour unique name: **{self.context_id}**\n\nYou can use this name to create guaranteed unique files in .scratch/ directory (e.g., `.scratch/{self.context_id}-output.txt`)."
            })

        # Initialize HEAD tracking if we have a worktree
        if self.worktree_path:
            self.last_head_commit = get_head_commit(cwd=self.worktree_path)

    def _load_system_prompt(self):
        """Load the system prompt from markdown files."""
        # Find the prompts directory (next to the script)
        script_dir = Path(__file__).parent
        prompts_dir = script_dir / 'prompts'

        # First load common.md (shared across all contexts)
        common_path = prompts_dir / 'common.md'
        if not common_path.exists():
            raise ContextError(f"Common prompt not found: {common_path}")

        common_prompt = common_path.read_text()
        self.messages.append({
            'role': 'system',
            'content': common_prompt
        })

        # Then load context-specific prompt
        prompt_file = f'{self.context_type}.md'
        prompt_path = prompts_dir / prompt_file

        if not prompt_path.exists():
            raise ContextError(f"System prompt not found: {prompt_path}")

        system_prompt = prompt_path.read_text()
        self.messages.append({
            'role': 'system',
            'content': system_prompt
        })

    def _load_agents_md(self):
        """Load AGENTS.md from the worktree if it exists."""
        if not self.worktree_path:
            return

        agents_path = self.worktree_path / 'AGENTS.md'
        if agents_path.exists():
            content = agents_path.read_text()
            self.agents_md_content = content
            self.messages.append({
                'role': 'system',
                'content': f"# Project Context (AGENTS.md)\n\n{content}"
            })

    def update_agents_md(self):
        """
        Check if AGENTS.md has been updated and append diff to context.

        This appends only the diff rather than full content to keep context small.

        TODO: Implement mechanism to remove older versions of AGENTS.md from context
        in a batch operation to prevent context from growing too large.
        """
        if not self.worktree_path:
            return False

        agents_path = self.worktree_path / 'AGENTS.md'
        if not agents_path.exists():
            return False

        new_content = agents_path.read_text()

        # Check if content has changed
        if new_content != self.agents_md_content:
            old_lines = self.agents_md_content.splitlines(keepends=True) if self.agents_md_content else []
            new_lines = new_content.splitlines(keepends=True)

            # Generate unified diff
            diff = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile='AGENTS.md (previous)',
                tofile='AGENTS.md (current)',
                lineterm=''
            )
            diff_text = '\n'.join(diff)

            self.agents_md_content = new_content

            # Append diff to keep caches active
            self.messages.append({
                'role': 'system',
                'content': f"# AGENTS.md Updated\n\nThe following changes were made to AGENTS.md:\n\n```diff\n{diff_text}\n```"
            })
            return True

        return False

    def _check_head_changes(self):
        """
        Check if HEAD has changed since last invocation.

        If changed, add a system message with commit info and changed files.
        """
        if not self.worktree_path or not self.last_head_commit:
            return

        current_head = get_head_commit(cwd=self.worktree_path)

        if current_head != self.last_head_commit:
            # HEAD has changed, gather info
            commits = get_commits_between(self.last_head_commit, current_head, cwd=self.worktree_path)
            changed_files = get_changed_files_between(self.last_head_commit, current_head, cwd=self.worktree_path)

            if commits or changed_files:
                # Build system message
                message_parts = ["# Repository Updates\n\nThe following changes have been made since you were last invoked:\n"]

                if commits:
                    message_parts.append("\n## New Commits\n")
                    for commit in commits:
                        message_parts.append(f"- `{commit['hash']}` {commit['message']}")

                if changed_files:
                    message_parts.append("\n\n## Changed Files\n")
                    for filepath in changed_files:
                        message_parts.append(f"- {filepath}")

                self.messages.append({
                    'role': 'system',
                    'content': '\n'.join(message_parts)
                })

            # Update tracking
            self.last_head_commit = current_head

    def add_message(self, role: str, content: str):
        """Add a message to the context."""
        self.messages.append({
            'role': role,
            'content': content
        })

    def get_tool_schemas(self) -> List[Dict]:
        """Get tool schemas for this context type."""
        if self.context_type == 'main':
            return tools.get_tool_schemas('main')
        else:
            return tools.get_tool_schemas('subcontext')

    def call_llm(self, logger=None, verbose=False) -> Dict[str, Any]:
        """
        Call the LLM and return the response.

        Args:
            logger: Optional session logger
            verbose: If True, print full prompts and responses

        Returns:
            Dict with 'message' and 'tool_calls' keys
        """
        # Check for HEAD changes before calling LLM
        self._check_head_changes()

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'HTTP-Referer': 'https://github.com/vanviegen/maca',
            'X-Title': 'MACA - Multi-Agent Coding Assistant'
        }

        # Get tool schemas
        tool_schemas = self.get_tool_schemas()

        data = {
            'model': self.model,
            'messages': self.messages,
            'tools': tool_schemas,
            'tool_choice': 'required',  # Force tool use
        }

        # Log full prompt
        if logger:
            logger.log_full_prompt(self.messages, self.context_id)

        # Display full prompt if verbose
        if verbose:
            from prompt_toolkit import print_formatted_text, HTML
            print_formatted_text(HTML("\n<ansiyellow>=== FULL PROMPT ===</ansiyellow>"))
            for i, msg in enumerate(self.messages):
                role = msg.get('role', 'unknown')
                content = msg.get('content', '')
                if content:
                    print_formatted_text(HTML(f"<ansicyan>[{i}] {role}:</ansicyan>"))
                    print_formatted_text(content)
                    print()
            print_formatted_text(HTML("<ansiyellow>===================</ansiyellow>\n"))

        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(data).encode('utf-8'),
                headers=headers
            )
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
        except Exception as e:
            if hasattr(e, 'read'):
                error_body = e.read().decode('utf-8')
                raise ContextError(f"LLM API error: {error_body}")
            raise ContextError(f"LLM API error: {str(e)}")

        # Log full response
        if logger:
            logger.log_full_response(result, self.context_id)

        # Display full response if verbose
        if verbose:
            from prompt_toolkit import print_formatted_text, HTML
            print_formatted_text(HTML("\n<ansiyellow>=== FULL RESPONSE ===</ansiyellow>"))
            response_str = json.dumps(result, indent=2)
            print_formatted_text(response_str)
            print_formatted_text(HTML("<ansiyellow>=====================</ansiyellow>\n"))

        # Extract response
        choice = result['choices'][0]
        message = choice['message']
        self.last_usage = result.get('usage', {})

        # Add assistant message to history
        self.messages.append(message)

        # Extract tool calls
        tool_calls = message.get('tool_calls', [])

        # Validate: must have exactly one tool call
        if len(tool_calls) != 1:
            raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")

        return {
            'message': message,
            'tool_call': tool_calls[0],
            'usage': self.last_usage
        }

    def execute_tool(self, tool_call: Dict) -> Any:
        """Execute a tool call and return the result."""
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        context_tool_type = 'main' if self.context_type == 'main' else 'subcontext'

        result = tools.execute_tool(tool_name, arguments, context_tool_type)
        return result

    def add_tool_result(self, tool_call: Dict, result: Any):
        """Add a tool result to the message history."""
        # Format tool result for the API
        self.messages.append({
            'role': 'tool',
            'tool_call_id': tool_call['id'],
            'content': str(result)
        })


class MainContext(BaseContext):
    """Main orchestrator context."""

    def __init__(self, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id='main',
            context_type='main',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class CodeAnalysisContext(BaseContext):
    """Context for analyzing code and creating/maintaining AGENTS.md."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='code_analysis',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class ResearchContext(BaseContext):
    """Context for web research and information gathering."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='research',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class ImplementationContext(BaseContext):
    """Context for implementing code based on specifications."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='implementation',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class ReviewContext(BaseContext):
    """Context for reviewing code quality and correctness."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='review',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class MergeContext(BaseContext):
    """Context for resolving merge conflicts."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='merge',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


class FileProcessorContext(BaseContext):
    """Context for processing individual files in batch operations."""

    def __init__(self, unique_name: str, model: str = "qwen/qwen3-coder-30b-a3b-instruct", api_key: Optional[str] = None, worktree_path: Optional[Path] = None):
        super().__init__(
            context_id=unique_name,
            context_type='file_processor',
            model=model,
            api_key=api_key,
            worktree_path=worktree_path
        )


# Context type registry for creation
CONTEXT_TYPES = {
    'code_analysis': CodeAnalysisContext,
    'research': ResearchContext,
    'implementation': ImplementationContext,
    'review': ReviewContext,
    'merge': MergeContext,
    'file_processor': FileProcessorContext
}


def create_context(unique_name: str, context_type: str, model: str = "auto", worktree_path: Optional[Path] = None) -> BaseContext:
    """
    Create a new context of the specified type.

    Args:
        unique_name: Unique identifier for the context
        context_type: Type of context to create
        model: Model to use (or "auto" for default)
        worktree_path: Path to the worktree

    Returns:
        New context instance
    """
    if context_type not in CONTEXT_TYPES:
        raise ContextError(f"Unknown context type: {context_type}")

    if model == "auto":
        # Use default model for each context type
        model = "anthropic/claude-sonnet-4.5"

    context_class = CONTEXT_TYPES[context_type]
    return context_class(unique_name, model, worktree_path=worktree_path)
