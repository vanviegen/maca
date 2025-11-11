#!/usr/bin/env python3
"""Context classes for managing different types of LLM interactions."""

import time
from pathlib import Path
from pathlib import Path
from typing import Dict, Any, Optional
import difflib
import json
import os
import urllib.request


class ContextError(Exception):
    """Context operation failed."""
    pass


class Context:
    """Context for managing LLM interactions."""

    instance_counters = {}

    def __init__(
        self,
        context_type: str,
        model: str = "auto",
        context_id=None,
        initial_message: Optional[str] = None
    ):
        """
        Initialize a context.

        Args:
            context_type: Type of context (main, code_analysis, research, etc.)
            model: Model to use for this context ("auto" to use default from prompt)
            initial_message: Optional initial user message to add to context
        """
        if not context_id:
            # Auto-generate unique name
            if context_type not in Context.instance_counters:
                Context.instance_counters[context_type] = 0
            Context.instance_counters[context_type] += 1
            context_id = f"{context_type}{Context.instance_counters[context_type]}"
            # Register this context
            maca.subcontexts[context_id] = self
        
        self.context_id = context_id
        self.context_type = context_type
        self.api_key = os.environ.get('OPENROUTER_API_KEY')
        self._messages = []
        self.cumulative_cost = 0
        self.agents_md_content = None
        self.last_head_commit = None
        self.default_model = 'openai/gpt-5-mini'

        self.logger = Logger(maca.repo_root, maca.session_id, self.context_id)

        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        # Load system prompt and parse metadata
        self.tool_names = []
        self._load_system_prompt()
        self.tool_schemas = tools.get_tool_schemas(self.tool_names)

        # Set model (use provided or default from prompt)
        if model == "auto":
            self.model = self.default_model
        else:
            self.model = model

        # Load AGENTS.md if it exists
        self._load_agents_md()

        # Add unique name info
        self.add_message({
            'role': 'system',
            'content': f"Your unique context identifier is: **{self.context_id}**"
        })

        # Initialize HEAD tracking if we have a worktree
        self.last_head_commit = git_ops.get_head_commit(cwd=maca.worktree_path)

        # Add initial message if provided
        if initial_message:
            self.add_message({'role': 'user', 'content': initial_message})

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

        self.add_message({
            'role': 'system',
            'content': common_prompt
        })

        # Then load context-specific prompt
        prompt_path = prompts_dir / f'{self.context_type}.md'
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
        
        self.add_message({
            'role': 'system',
            'content': system_prompt
        })

    def _load_agents_md(self):
        """Load AGENTS.md from the worktree if it exists."""
        if not maca.worktree_path:
            return

        agents_path = maca.worktree_path / 'AGENTS.md'
        if agents_path.exists():
            content = agents_path.read_text()
            self.agents_md_content = content
            self.add_message({
                'role': 'system',
                'content': f"# Project Context (AGENTS.md)\n\n{content}"
            })

    def _diff_agents_md(self):
        """
        Check if AGENTS.md has been updated and append diff to context.

        This appends only the diff rather than full content to keep context small.
        """
        agents_path = maca.worktree_path / 'AGENTS.md'
        if not agents_path.exists():
            return False

        new_content = agents_path.read_text()

        # Check if content has changed
        if new_content == self.agents_md_content:
            return False
        
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
        self.add_message({
            'role': 'system',
            'content': f"# AGENTS.md Updated\n\nThe following changes were made to AGENTS.md:\n\n```diff\n{diff_text}\n```"
        })
        return True

    def _check_head_changes(self):
        """
        Check if HEAD has changed since last invocation.

        If changed, add a system message with commit info and changed files.
        """
        if not maca.worktree_path or not self.last_head_commit:
            return

        current_head = git_ops.get_head_commit(cwd=maca.worktree_path)

        if current_head != self.last_head_commit:
            # HEAD has changed, gather info
            commits = git_ops.get_commits_between(self.last_head_commit, current_head, cwd=maca.worktree_path)
            changed_files = git_ops.get_changed_files_between(self.last_head_commit, current_head, cwd=maca.worktree_path)

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

                self.add_message({
                    'role': 'system',
                    'content': '\n'.join(message_parts)
                })

            # Update tracking
            self.last_head_commit = current_head

    def call_llm(self) -> Dict[str, Any]:
        """
        Call the LLM and return the response.

        Returns:
            Dict with 'message' and 'tool_calls' keys
        """

        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'HTTP-Referer': 'https://github.com/vanviegen/maca',
            'X-Title': 'MACA - Multi-Agent Coding Assistant'
        }

        data = {
            'model': self.model,
            'messages': self._messages,
            'tools': self.tool_schemas,
            'usage': {"include": True},
            'tool_choice': 'required',  # Force tool use
        }
        start_time = time.time()

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

        self.logger.log(tag='llm_call', model=self.model, cost=cost, prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'], duration=time.time() - start_time)
        self.logger.log(tag='full_response', **result) # debugging only

        # Add assistant message to history
        self.add_message(message)

        return {
            'message': message,
            'cost': cost
        }
    
    def add_message(self, message: Dict):
        """Add a message dict to the context and the log."""
        self.logger.log(tag="message", **message)
        self._messages.append(message)

    def run(self, budget=None):
        """
        Run this context until completion or budget exceeded.

        Args:
            budget: Maximum cost in microdollars before returning (None = unlimited)

        Returns: (str)
            dict with:
            - completed: bool (True if completed normally, False if budget exceeded)
            - summary: str (summary of what happened)
            - cost: int (total cost in microdollars for this run)
        """
        total_cost = 0
        completed = False
        summary_parts = []
        is_subcontext = (self.context_type != '_main')
        indent = '  ' if is_subcontext else ''

        # Loop until completion or budget exceeded
        while not completed:
            # Print thinking message
            color_print(('ansicyan', f"{indent}Context '{self.context_id}' thinking..."))

            # Check if AGENTS.md was updated and refresh all contexts
            if self._diff_agents_md():
                color_print(indent, ('ansicyan', 'AGENTS.md updated in context'))

            # Check for HEAD changes before calling LLM
            self._check_head_changes()

            # Call LLM
            for _ in range(3):  # Retry up to 3 times
                try:
                    result = self.call_llm()
                    break
                except Exception as err:
                    color_print(indent, ('ansired', f"Error during LLM call: {err}. Retrying..."))
                    summary_parts.append(f"Error during LLM call: {err}")
                    self.logger.log(tag='error', error=str(err))
            else:
                break

            # Extract tool calls
            message = result['message']
            cost = result['cost']
            total_cost += cost

            # Extract tool info
            tool_calls = message.get('tool_calls', [])
            if len(tool_calls) != 1:
                raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")
            tool_call = tool_calls[0]
            tool_name = tool_call['function']['name']
            tool_args = json.loads(tool_call['function']['arguments'])

            # Print tool info
            color_print(indent, ('ansigreen', '→'), ' Tool: ', ('ansiyellow', f"{tool_name}({tool_args})"))

            # Print rationale if present (subcontexts only)
            rationale = tool_args.get('rationale', '')
            if rationale:
                color_print(f"    Rationale: {rationale}")

            # Execute tool
            tool_start = time.time()
            try:
                result = tools.execute_tool(tool_name, tool_args)
                tool_duration = time.time() - tool_start
            except Exception as err:
                result = {"error": str(err)}

            if isinstance(result, tools.ReadyResult):
                result = result.result
                completed = True

            self.logger.log(tag='tool_call', tool=tool_name, args=tool_args, duration=tool_duration, result=result, completed=completed)

            self.add_message({
                'role': 'tool',
                'tool_call_id': tool_call['id'],
                'content': result if isinstance(result, str) else json.dumps(result)
            })

            # Check for git changes and commit if needed
            diff_stats = git_ops.get_diff_stats(maca.worktree_path)
            if diff_stats:
                # Commit changes
                commit_msg = f'{tool_name}: {rationale}' if rationale else tool_name
                git_ops.commit_changes(maca.worktree_path, commit_msg)
                self.logger.log(tag='commit', message=commit_msg, diff_stats=diff_stats)
                color_print(indent, ('ansigreen', '✓ Committed changes'))

            # Build summary for this iteration
            abbr_args = json.dumps(tool_args)
            if len(abbr_args) > 120:
                abbr_args = abbr_args[:80] + '...'
            iteration_summary = f"Called {tool_name}({abbr_args}) because: {rationale}"
            summary_parts.append(iteration_summary)

            if completed:
                color_print(indent, ('ansigreen', f'✓ Context {self.context_id} completed. Cost: {total_cost}μ$'))
                self.logger.log(tag='complete')
                summary_parts.append(result)
                break

            # Check budget (only for subcontexts)
            if budget is not None and total_cost > budget:
                budget_msg = f"Context '{self.context_id}' budget exceeded (spent {total_cost}μ$ of {budget}μ$)"
                color_print(indent, ('ansiyellow', budget_msg))
                summary_parts.append(budget_msg)
                break

        return {
            'summary': "\n".join(summary_parts),
            'completed': True,
            'cost': total_cost
        }



from logger import Logger
from maca import maca
from utils import color_print
import tools
import git_ops
