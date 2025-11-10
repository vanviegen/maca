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


class Context:
    """Context for managing LLM interactions."""

    def __init__(
        self,
        context_id: str,
        context_type: str,
        model: str = "auto",
        api_key: Optional[str] = None,
        worktree_path: Optional[Path] = None
    ):
        """
        Initialize a context.

        Args:
            context_id: Unique identifier for this context
            context_type: Type of context (main, code_analysis, research, etc.)
            model: Model to use for this context ("auto" to use default from prompt)
            api_key: OpenRouter API key (defaults to env var)
            worktree_path: Path to the worktree (for loading AGENTS.md)
        """
        self.context_id = context_id
        self.context_type = context_type
        self.api_key = api_key or os.environ.get('OPENROUTER_API_KEY')
        self.worktree_path = worktree_path
        self.messages = []
        self.cumulative_cost = 0
        self.agents_md_content = None
        self.last_head_commit = None
        self.default_model = 'openai/gpt-5-mini'
        self.tool_names = []

        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        # Load system prompt and parse metadata
        self._load_system_prompt()

        # Set model (use provided or default from prompt)
        if model == "auto":
            self.model = self.default_model
        else:
            self.model = model

        # Load AGENTS.md if it exists
        self._load_agents_md()

        # Add unique name info for subcontexts (main context doesn't need this)
        if self.context_type != '_main':
            self.messages.append({
                'role': 'system',
                'content': f"# Your Context Info\n\nYour unique name: **{self.context_id}**\n\nYou can use this name to create guaranteed unique files in .scratch/ directory (e.g., `.scratch/{self.context_id}-output.txt`)."
            })

        # Initialize HEAD tracking if we have a worktree
        if self.worktree_path:
            self.last_head_commit = get_head_commit(cwd=self.worktree_path)

    def _load_system_prompt(self):
        """Load the system prompt from markdown files and parse metadata."""
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

        prompt_content = prompt_path.read_text()

        # Split into headers and prompt body on first blank line
        parts = prompt_content.split('\n\n', 1)
        if len(parts) != 2:
            raise ContextError(f"Prompt file {prompt_path} must have headers separated by blank line")
        
        headers_text, system_prompt = parts
        
        # Parse headers
        for line in headers_text.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            if ':' not in line:
                raise ContextError(f"Invalid header format in {prompt_path}: {line}")
            
            key, value = line.split(':', 1)
            key = key.strip()
            value = value.strip()
            
            if key == 'default_model':
                self.default_model = value
            elif key == 'tools':
                self.tool_names = [name.strip() for name in value.split(',')]
            else:
                raise ContextError(f"Unknown header key in {prompt_path}: {key}")
        
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
        """Get tool schemas for this context."""
        # Add rationale to tools for non-main contexts (subcontexts need rationale)
        add_rationale = (self.context_type != '_main')
        return tools.get_tool_schemas(self.tool_names, add_rationale=add_rationale)

    def call_llm(self, logger=None) -> Dict[str, Any]:
        """
        Call the LLM and return the response.

        Args:
            logger: Optional session logger

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
            'usage': {"include": True},
            'tool_choice': 'required',  # Force tool use
        }

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

        # Extract response
        choice = result['choices'][0]
        message = choice['message']

        # Extract usage
        usage = result.get('usage', {})
        cost = int(usage.get('cost', 0) * 1_000_000)  # Convert dollars to microdollars
        self.cumulative_cost += cost

        # Add assistant message to history
        self.messages.append(message)

        # Extract tool calls
        tool_calls = message.get('tool_calls', [])

        # Validate: must have exactly one tool call
        if len(tool_calls) != 1:
            raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")

        if logger:
            logger.log(self.context_id, type='response', message=message, cost=cost, tool_call=tool_calls[0])

        return {
            'message': message,
            'tool_call': tool_calls[0],
            'usage': usage
        }

    def execute_tool(self, tool_call: Dict) -> Any:
        """Execute a tool call and return the result."""
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        result = tools.execute_tool(tool_name, arguments)
        return result

    def add_tool_result(self, tool_call: Dict, result: Any):
        """Add a tool result to the message history."""
        # Format tool result for the API
        self.messages.append({
            'role': 'tool',
            'tool_call_id': tool_call['id'],
            'content': str(result)
        })

    def run(self, budget=None, logger=None, single_iteration=False, maca=None):
        """
        Run this context until completion, budget exceeded, or single iteration complete.

        Args:
            budget: Maximum cost in microdollars before returning (None = unlimited)
            logger: Session logger instance
            single_iteration: If True, run only one LLM call + tool execution then return
            maca: MACA instance (for refreshing AGENTS.md across all contexts)

        Returns:
            dict with:
            - completed: bool (True if completed normally, False if budget exceeded)
            - summary: str (summary of what happened)
            - cost: int (total cost in microdollars for this run)
            - tool_name: str (name of last tool executed)
            - tool_result: Any (result of last tool execution)
        """
        import time
        import json
        from prompt_toolkit import print_formatted_text
        from prompt_toolkit.formatted_text import FormattedText
        import git_ops

        total_cost = 0
        spent = 0
        summary_parts = []
        last_tool_name = None
        last_tool_result = None
        is_subcontext = (self.context_type != '_main')
        indent = is_subcontext

        # Loop until completion or budget exceeded
        while True:
            # Print thinking message
            prefix = '  ' if indent else ''
            thinking_msg = f"{prefix}Subcontext '{self.context_id}' thinking..." if indent else 'Main context thinking ...'
            print_formatted_text(FormattedText([('ansicyan', thinking_msg)]))

            # Call LLM
            try:
                response = self.call_llm(logger=logger)
            except Exception as e:
                if logger:
                    logger.log(self.context_id, type='error', error=str(e))
                return {
                    'completed': False,
                    'summary': f"Error during LLM call: {str(e)}",
                    'cost': total_cost,
                    'tool_name': last_tool_name,
                    'tool_result': last_tool_result
                }

            tool_call = response['tool_call']
            usage = response['usage']

            # Extract usage metrics
            tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
            cost = usage.get('cost', 0)
            total_cost += cost
            spent += cost

            # Log LLM call
            if logger:
                logger.log(self.context_id, type='llm_call', model=self.model, tokens=tokens, cost=cost)

            # Extract tool info
            tool_name = tool_call['function']['name']
            arguments = json.loads(tool_call['function']['arguments'])
            last_tool_name = tool_name

            # Log tool call
            if logger:
                logger.log(self.context_id, type='tool_call', tool_name=tool_name, arguments=str(arguments))

            # Print tool info
            arrow_prefix = '  ' if indent else ''
            print_formatted_text(FormattedText([
                ('', arrow_prefix),
                ('ansigreen', '→'),
                ('', ' Tool: '),
                ('ansiyellow', tool_name),
            ]))

            # Print rationale if present (subcontexts only)
            rationale = arguments.get('rationale', '')
            if indent and rationale:
                print_formatted_text(FormattedText([
                    ('', f"    Rationale: {rationale}"),
                ]))

            # Execute tool
            tool_start = time.time()
            try:
                result = self.execute_tool(tool_call)
                last_tool_result = result
                tool_duration = time.time() - tool_start
            except Exception as e:
                error_msg = f"Error executing tool {tool_name}: {str(e)}"
                if logger:
                    logger.log(self.context_id, type='error', error=error_msg)
                # Add error as tool result to maintain conversation integrity
                self.add_tool_result(tool_call, error_msg)
                return {
                    'completed': False,
                    'summary': error_msg,
                    'cost': total_cost,
                    'tool_name': tool_name,
                    'tool_result': None
                }

            # Log tool result
            if logger:
                logger.log(self.context_id, type='tool_result', tool_name=tool_name, result=str(result), duration=tool_duration)

            # Check if tool returned True (completion signal)
            completed = (result is True)

            # For completion tools, extract the result from arguments
            if completed:
                result = arguments.get('result', 'Task completed')
                last_tool_result = result

            # Add tool result to context
            self.add_tool_result(tool_call, str(result))

            # Check for git changes and commit if needed
            diff_stats = None
            if hasattr(tools, 'WORKTREE_PATH') and tools.WORKTREE_PATH:
                from pathlib import Path
                worktree_path = Path(tools.WORKTREE_PATH)
                diff_stats = git_ops.get_diff_stats(worktree_path)

                if diff_stats:
                    # Commit changes
                    commit_msg = rationale or f'{tool_name} executed'
                    git_ops.commit_changes(worktree_path, commit_msg)
                    if logger:
                        logger.log(self.context_id, type='commit', message=commit_msg, diff_stats=diff_stats)

                    print_formatted_text(FormattedText([
                        ('', '    ' if indent else ''),
                        ('ansigreen', '✓ Committed changes'),
                    ]))

                    # Check if AGENTS.md was updated and refresh all contexts
                    if self.update_agents_md():
                        print_formatted_text(FormattedText([
                            ('', '    ' if indent else ''),
                            ('ansicyan', 'AGENTS.md updated - refreshed all contexts'),
                        ]))
                        # Update all contexts if maca instance available
                        if maca:
                            if hasattr(maca, 'main_context') and maca.main_context and maca.main_context != self:
                                maca.main_context.update_agents_md()
                            for ctx in getattr(maca, 'subcontexts', {}).values():
                                if ctx != self:
                                    ctx.update_agents_md()

            # Build summary for this iteration
            iteration_summary = f"Tool: {tool_name}\n"
            if rationale:
                iteration_summary += f"Rationale: {rationale}\n"
            iteration_summary += f"Tokens: {tokens}, Cost: {cost}μ$, Duration: {tool_duration:.2f}s\n"
            if diff_stats:
                iteration_summary += f"Changes:\n{diff_stats}\n"
            iteration_summary += f"Result: {str(result)[:500]}\n"
            summary_parts.append(iteration_summary)

            # Check if we should return
            if completed:
                print_formatted_text(FormattedText([
                    ('', '    ' if indent else ''),
                    ('ansigreen', f'✓ Context {self.context_id} completed. Cost: {total_cost}μ$'),
                ]))
                return {
                    'completed': True,
                    'summary': '\n'.join(summary_parts),
                    'cost': total_cost,
                    'tool_name': tool_name,
                    'tool_result': result
                }

            # Check budget (only for subcontexts)
            if budget is not None:
                remaining = budget - spent
                if remaining <= 0:
                    budget_msg = f"Context '{self.context_id}' budget exceeded (spent {spent}μ$ of {budget}μ$)"
                    print_formatted_text(FormattedText([
                        ('', '    ' if indent else ''),
                        ('ansiyellow', budget_msg),
                    ]))
                    return {
                        'completed': False,
                        'summary': '\n'.join(summary_parts) + f"\n\n{budget_msg}",
                        'cost': total_cost,
                        'tool_name': tool_name,
                        'tool_result': result
                    }

                # Show remaining budget
                print_formatted_text(FormattedText([
                    ('', '    ' if indent else ''),
                    ('ansiblue', f'Budget remaining: {remaining}μ$'),
                ]))

            # If single_iteration mode, return after one iteration
            if single_iteration:
                return {
                    'completed': False,
                    'summary': '\n'.join(summary_parts),
                    'cost': total_cost,
                    'tool_name': tool_name,
                    'tool_result': result
                }
