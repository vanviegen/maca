#!/usr/bin/env python3
"""Multi-Agent Coding Assistant - Main entry point."""

import argparse
import sys
import os
import time
from pathlib import Path
from typing import Dict, Optional
import json

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory

import git_ops
import contexts
import tools
from session_logging import SessionLogger


# Global state that tools need access to
tools.WORKTREE_PATH = None
tools.REPO_ROOT = None

# Setup history file
HISTORY_FILE = Path.home() / '.maca' / 'history'
HISTORY_FILE.parent.mkdir(exist_ok=True)
HISTORY = FileHistory(str(HISTORY_FILE))


class MACA:
    """Main orchestration class for the multi-agent coding assistant."""

    def __init__(self, repo_path: str = '.'):
        """
        Initialize the assistant.

        Args:
            repo_path: Path to the git repository
        """
        self.repo_path = Path(repo_path).resolve()
        self.repo_root = None
        self.session_id = None
        self.worktree_path = None
        self.branch_name = None
        self.logger = None
        self.main_context = None
        self.subcontexts: Dict[str, contexts.BaseContext] = {}
        self.context_counters: Dict[str, int] = {}  # Track counter per context type for auto-naming
        self.subcontext_budgets: Dict[str, int] = {}  # Budget in μ$ per subcontext
        self.subcontext_spent: Dict[str, int] = {}  # Amount spent in μ$ per subcontext

    def ensure_git_repo(self):
        """Ensure we're in a git repository, or offer to initialize one."""
        if not git_ops.is_git_repo(self.repo_path):
            print_formatted_text(FormattedText([('ansired', 'Not in a git repository.')]))

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
            print_formatted_text(FormattedText([('ansigreen', 'Git repository initialized.')]))

        self.repo_root = git_ops.get_repo_root(self.repo_path)

    def create_session(self):
        """Create a new session with worktree and branch."""
        # Find next session ID
        self.session_id = git_ops.find_next_session_id(self.repo_root)

        # Create worktree and branch
        self.worktree_path, self.branch_name = git_ops.create_session_worktree(
            self.repo_root, self.session_id
        )

        # Set global state for tools
        tools.WORKTREE_PATH = str(self.worktree_path)
        tools.REPO_ROOT = str(self.repo_root)
        tools.MACA_INSTANCE = self

        # Initialize session logger
        self.logger = SessionLogger(self.repo_root, self.session_id)

        print_formatted_text(FormattedText([
            ('ansigreen', f'Session {self.session_id} created'),
            ('', ' (branch: '),
            ('ansicyan', self.branch_name),
            ('', ', worktree: '),
            ('ansicyan', str(self.worktree_path.relative_to(self.repo_root))),
            ('', ')'),
        ]))

    def get_initial_prompt(self, prompt_arg: Optional[str] = None) -> str:
        """Get the initial prompt from user."""
        if prompt_arg:
            return prompt_arg

        while True:
            print_formatted_text(FormattedText([('ansiyellow', 'Enter your task (press Alt+Enter or Esc+Enter to submit):')]))

            prompt = pt_prompt("> ", multiline=True, history=HISTORY).strip()

            return prompt

    def _execute_llm_call_and_log(self, context, context_id: str, indent: bool = False) -> tuple:
        """
        Execute LLM call and log it. Common logic for both main and subcontexts.

        Args:
            context: The context object to call
            context_id: The ID to use for logging (e.g., 'main' or unique_name)
            indent: Whether to indent the output messages

        Returns:
            Tuple of (tool_call, arguments, tokens, cost)
        """
        # Print thinking message
        prefix = '  ' if indent else ''
        thinking_msg = f"{prefix}Subcontext '{context_id}' thinking..." if indent else 'Main context thinking ...'
        print_formatted_text(FormattedText([('ansicyan', thinking_msg)]))

        # Call LLM
        response = context.call_llm(logger=self.logger)
        tool_call = response['tool_call']
        usage = response['usage']

        # Extract usage metrics
        tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
        cost = usage.get('cost', 0)

        # Log LLM call
        self.logger.log(context_id, type='llm_call', model=context.model, tokens=tokens, cost=cost)

        # Extract tool info
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        # Log tool call
        self.logger.log(context_id, type='tool_call', tool_name=tool_name, arguments=str(arguments))

        # Print tool info
        arrow_prefix = '  ' if indent else ''
        print_formatted_text(FormattedText([
            ('', arrow_prefix),
            ('ansigreen', '→'),
            ('', ' Tool: '),
            ('ansiyellow', tool_name),
        ]))

        # Print rationale if present (subcontexts only)
        if indent and 'rationale' in arguments:
            print_formatted_text(FormattedText([
                ('', f"    Rationale: {arguments['rationale']}"),
            ]))

        return tool_call, arguments, tokens, cost

    def run_main_context(self):
        """Run one iteration of the main context."""
        try:
            tool_call, _, _, _ = self._execute_llm_call_and_log(
                self.main_context, 'main', indent=False
            )
            return tool_call

        except Exception as e:
            self.logger.log('main', type='error', error=str(e))
            raise

    def execute_main_tool(self, tool_call: Dict) -> tuple:
        """
        Execute a main context tool call.

        Returns:
            Tuple of (result_string, should_run_subcontext, subcontext_name)
        """
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        # Special case: complete() signals end of task
        if tool_name == 'complete':
            return ('COMPLETE', False, None)

        # All other tools are executed normally via the tool system
        result = tools.execute_tool(tool_name, arguments, context_type='main')
        self.logger.log('main', type='tool_result', tool_name=tool_name, result=str(result), duration=0)

        # For run_oneshot_per_file, we need to run the first file processor subcontext
        # This is the only remaining special case
        if tool_name == 'run_oneshot_per_file' and 'first_subcontext' in result:
            # Extract first subcontext name from result dict
            return (str(result['message']), True, result['first_subcontext'])

        return (str(result), False, None)

    def run_subcontext(self, unique_name: str):
        """
        Run a subcontext autonomously until budget is exceeded or it calls complete().

        The subcontext will loop, executing tool calls until either:
        1. It calls complete() - task is done
        2. Budget is exceeded - control returns to main for verification
        3. An error occurs

        All tool calls and rationales are still logged to main context for monitoring.
        """
        subcontext = self.subcontexts[unique_name]
        budget = self.subcontext_budgets.get(unique_name, 20000)
        spent = self.subcontext_spent.get(unique_name, 0)

        # Loop until budget exceeded or complete() called
        while True:
            tool_call = None

            try:
                # Execute LLM call and log it
                tool_call, arguments, tokens, cost = self._execute_llm_call_and_log(
                    subcontext, unique_name, indent=True
                )
                tool_name = tool_call['function']['name']

                # Update spending
                spent += cost
                self.subcontext_spent[unique_name] = spent

                # Execute tool
                tool_start = time.time()

                if tool_name == 'complete':
                    result = arguments.get('result', 'Task completed')
                    tool_duration = 0
                else:
                    result = subcontext.execute_tool(tool_call)
                    tool_duration = time.time() - tool_start

                self.logger.log(unique_name, type='tool_result', tool_name=tool_name, result=result, duration=tool_duration)

                # Add tool result to subcontext
                subcontext.add_tool_result(tool_call, result)

                # Check for git changes and commit if needed
                diff_stats = git_ops.get_diff_stats(self.worktree_path)

                agents_md_updated = False
                if diff_stats:
                    # Commit changes
                    commit_msg = arguments.get('rationale', f'{tool_name} executed')
                    git_ops.commit_changes(self.worktree_path, commit_msg)
                    self.logger.log(unique_name, type='commit', message=commit_msg, diff_stats=diff_stats)

                    print_formatted_text(FormattedText([
                        ('', '    '),
                        ('ansigreen', '✓ Committed changes'),
                    ]))

                    # Check if AGENTS.md was updated and refresh all contexts
                    if self.main_context.update_agents_md():
                        agents_md_updated = True
                        print_formatted_text(FormattedText([
                            ('', '    '),
                            ('ansicyan', 'AGENTS.md updated - refreshed all contexts'),
                        ]))
                        # Update all subcontexts too
                        for ctx in self.subcontexts.values():
                            ctx.update_agents_md()

                # Build summary for main context (for monitoring)
                summary = self._build_subcontext_summary(
                    unique_name, tool_name, arguments.get('rationale', ''),
                    tokens, cost, tool_duration, diff_stats, result
                )

                # If this was an implementation context and AGENTS.md was updated, ask main to verify
                if subcontext.context_type == 'implementation' and agents_md_updated:
                    summary += ("\n\nNote: AGENTS.md was updated by this implementation. Please verify from "
                               "a high-level perspective whether this update was appropriate and necessary. "
                               "You may query the subcontext for the rationale behind the updates if needed.")

                # Add summary to main context (for monitoring)
                self.main_context.add_message('user', summary)
                self.logger.log(unique_name, type='subcontext_summary', tool=tool_name, tokens=tokens, cost=cost, duration=tool_duration, diff_stats=diff_stats, agents_md_updated=agents_md_updated)

                # Check if we should return control to main
                if tool_name == 'complete':
                    print_formatted_text(FormattedText([
                        ('', '    '),
                        ('ansigreen', f'✓ Subcontext {unique_name} completed. Spent: {spent}μ$'),
                    ]))
                    break

                remaining = budget - spent
                if remaining <= 0:
                    budget_msg = f"Subcontext '{unique_name}' budget exceeded (spent {spent}μ$ of {budget}μ$). Returning control to main."
                    print_formatted_text(FormattedText([
                        ('', '    '),
                        ('ansiyellow', budget_msg),
                    ]))
                    self.main_context.add_message('user', budget_msg)
                    break

                # Show remaining budget
                print_formatted_text(FormattedText([
                    ('', '    '),
                    ('ansiblue', f'Budget remaining: {remaining}μ$'),
                ]))

            except Exception as e:
                self.logger.log(unique_name, type='error', error=str(e))
                error_msg = f"Error in subcontext '{unique_name}': {str(e)}"

                # CRITICAL: If we got a tool_call but hit an error before adding the result,
                # we MUST add an error result to maintain conversation history integrity
                if tool_call is not None:
                    try:
                        subcontext.add_tool_result(tool_call, f"Error: {str(e)}")
                    except:
                        pass  # If this fails, we're in a bad state anyway

                self.main_context.add_message('user', error_msg)
                print_formatted_text(FormattedText([('ansired', error_msg)]))
                break  # Exit loop on error

    def _generate_unique_context_name(self, context_type: str) -> str:
        """
        Generate a unique name for a context by auto-incrementing a counter.

        Args:
            context_type: Type of context (e.g., 'code_analysis', 'implementation', etc.)

        Returns:
            Unique name like 'code_analysis1', 'implementation2', etc.
        """
        if context_type not in self.context_counters:
            self.context_counters[context_type] = 0
        self.context_counters[context_type] += 1
        return f"{context_type}{self.context_counters[context_type]}"

    def _create_and_register_subcontext(
        self, unique_name: str, context_type: str, model: str, initial_message: str = None
    ) -> contexts.BaseContext:
        """
        Create a subcontext and register it.

        Args:
            unique_name: Unique identifier for this context
            context_type: Type of context to create
            model: Model to use for this context
            initial_message: Optional initial user message to add to context

        Returns:
            The created subcontext
        """
        subcontext = contexts.Context(
            context_id=unique_name,
            context_type=context_type,
            model=model,
            worktree_path=self.worktree_path
        )
        self.subcontexts[unique_name] = subcontext

        if initial_message:
            subcontext.add_message('user', initial_message)

        return subcontext

    def _build_subcontext_summary(
        self, unique_name: str, tool_name: str, rationale: str,
        tokens: int, cost: int, duration: float, diff_stats: str, result: any
    ) -> str:
        """Build a summary message to send back to main context."""
        summary = f"Subcontext '{unique_name}' executed tool: {tool_name}\n\n"
        summary += f"Rationale: {rationale}\n\n"
        summary += f"Tokens used: {tokens}, Cost: {cost}μ$, Duration: {duration:.2f}s\n\n"

        if diff_stats:
            summary += f"Changes made (git diff --numstat):\n{diff_stats}\n\n"
        else:
            summary += "No file changes made.\n\n"

        # Include result summary (truncated)
        result_str = str(result)[:500]
        summary += f"Result: {result_str}\n\n"

        summary += "Do you want to continue this subcontext or take a different action?"

        return summary

    def handle_completion(self, result: str) -> bool:
        """
        Handle task completion. Returns True if user approves, False otherwise.

        Args:
            result: The completion result from main context

        Returns:
            True if approved and merged, False if user wants changes
        """
        print_formatted_text(FormattedText([
            ('', '\n'),
            ('ansigreen', 'Task completed!'),
            ('', f'\n{result}\n'),
        ]))

        # Ask for approval
        response = radiolist_dialog(
            title='Task Complete',
            text='Are you satisfied with the result?',
            values=[
                ('yes', 'Yes, merge into main'),
                ('no', 'No, I want changes'),
                ('cancel', 'Cancel (keep worktree for manual review)')
            ]
        ).run()

        if response == 'yes':
            self.merge_and_cleanup()
            return True
        elif response == 'no':
            feedback = pt_prompt("What changes do you want?\n> ", multiline=True, history=HISTORY)
            self.main_context.add_message('user', feedback)
            return False
        else:
            print("Keeping worktree for manual review.")
            sys.exit(0)

    def merge_and_cleanup(self):
        """Merge the session branch into main and cleanup."""
        print_formatted_text(FormattedText([('ansicyan', 'Merging changes...')]))

        # Get a commit message
        commit_msg = pt_prompt("Enter commit message for the squashed commit:\n> ", multiline=True, history=HISTORY)

        # Merge
        success, message = git_ops.merge_to_main(
            self.repo_root,
            self.worktree_path,
            self.branch_name,
            commit_msg
        )

        if not success:
            print_formatted_text(FormattedText([('ansired', f'Merge failed: {message}')]))
            print("You may need to resolve conflicts manually or spawn a merge context.")
            # TODO: Spawn merge context here
            sys.exit(1)

        # Cleanup
        git_ops.cleanup_session(self.repo_root, self.worktree_path, self.branch_name)

        print_formatted_text(FormattedText([('ansigreen', '✓ Merged and cleaned up')]))

        # Reset session for next task
        self.create_session()

    def run(self, initial_prompt: Optional[str] = None):
        """
        Main orchestration loop.

        Args:
            initial_prompt: Optional initial prompt from command line
        """
        # Ensure git repo
        self.ensure_git_repo()

        # Create session
        self.create_session()

        # Initialize main context
        self.main_context = contexts.Context(
            context_id='main',
            context_type='_main',
            worktree_path=self.worktree_path
        )

        # Auto-call list_files for top-level directory to give context about project structure
        try:
            top_files_result = tools.execute_tool('list_files', {'path_regex': r'^[^/\\]*$'})
            # Add as a system message so main context knows what files are in the top directory
            top_files_msg = f"Top-level directory contains {top_files_result['total_count']} files"
            if top_files_result['files']:
                top_files_msg += f":\n" + "\n".join(f"- {f}" for f in top_files_result['files'])
            self.main_context.add_message('system', top_files_msg)
        except Exception as e:
            # Don't fail if this doesn't work
            pass

        # Check if AGENTS.md exists, if not suggest creating it
        self.first_llm_call = True

        # Main loop
        while True:
            # Get initial prompt if this is a new task
            prompt = self.get_initial_prompt(initial_prompt)
            initial_prompt = None  # Only use command line arg for first iteration

            if not prompt:
                print("No task provided. Exiting.")
                break

            # On first LLM call, if no AGENTS.md exists, prepend guidance to create it
            agents_path = self.worktree_path / 'AGENTS.md'
            if self.first_llm_call and not agents_path.exists():
                guidance = ("Note: This project does not have an AGENTS.md file yet. Unless the user's "
                           "request explicitly does not benefit from understanding the codebase, or they "
                           "explicitly ask not to, your first step should usually be to create a code_analysis "
                           "subcontext to analyze the project and create an initial AGENTS.md file. This file "
                           "should be short and lean, documenting key project context, architecture, and dependencies.")
                self.main_context.add_message('user', f"{guidance}\n\nUser request: {prompt}")
            else:
                # Add to main context
                self.main_context.add_message('user', prompt)

            # self.logger.log('main', type='prompt', prompt=prompt)
            self.first_llm_call = False

            # Main context loop
            while True:
                # Run main context
                tool_call = self.run_main_context()

                # Execute the tool
                result, should_run_subcontext, subcontext_name = self.execute_main_tool(tool_call)

                if result == 'COMPLETE':
                    # Handle completion
                    arguments = json.loads(tool_call['function']['arguments'])
                    completion_result = arguments.get('result', 'Task completed')

                    approved = self.handle_completion(completion_result)

                    if approved:
                        # Merged successfully, start new task
                        break
                    else:
                        # User wants changes, continue main loop
                        continue

                # Add result to main context BEFORE running subcontext
                self.main_context.add_tool_result(tool_call, result)

                # Now run the subcontext if needed
                if should_run_subcontext:
                    self.run_subcontext(subcontext_name)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='maca',
        description='Multi-Agent Coding Assistant - A multi-context AI coding assistant',
    )
    parser.add_argument('task', nargs='*', help='Initial task description')
    parser.add_argument('-m', '--model', default='anthropic/claude-sonnet-4.5',
                        help='Model to use for main context')
    parser.add_argument('-d', '--directory', default='.',
                        help='Project directory (default: current directory)')

    args = parser.parse_args()

    # Combine task arguments
    task = ' '.join(args.task) if args.task else None

    # Create and run assistant
    assistant = MACA(args.directory)
    assistant.run(task)


if __name__ == '__main__':
    main()
