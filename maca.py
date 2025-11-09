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
from prompt_toolkit import print_formatted_text, HTML
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
        self.verbose = False

    def ensure_git_repo(self):
        """Ensure we're in a git repository, or offer to initialize one."""
        if not git_ops.is_git_repo(self.repo_path):
            print_formatted_text(HTML("<ansired>Not in a git repository.</ansired>"))

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
            print_formatted_text(HTML("<ansigreen>Git repository initialized.</ansigreen>"))

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

        # Initialize session logger
        self.logger = SessionLogger(self.repo_root, self.session_id)

        print_formatted_text(HTML(
            f"<ansigreen>Session {self.session_id} created</ansigreen> "
            f"(branch: <ansicyan>{self.branch_name}</ansicyan>, "
            f"worktree: <ansicyan>{self.worktree_path.relative_to(self.repo_root)}</ansicyan>)"
        ))

    def get_initial_prompt(self, prompt_arg: Optional[str] = None) -> str:
        """Get the initial prompt from user."""
        if prompt_arg:
            return prompt_arg

        while True:
            print_formatted_text(HTML("<ansiyellow>Enter your task (press Alt+Enter or Esc+Enter to submit):</ansiyellow>"))
            if self.verbose:
                print_formatted_text(HTML("<ansicyan>[Verbose mode: ON]</ansicyan>"))

            prompt = pt_prompt("> ", multiline=True, history=HISTORY).strip()

            # Check for special commands
            if prompt == '/verbose on':
                self.verbose = True
                print_formatted_text(HTML("<ansigreen>Verbose mode enabled</ansigreen>"))
                continue
            elif prompt == '/verbose off':
                self.verbose = False
                print_formatted_text(HTML("<ansigreen>Verbose mode disabled</ansigreen>"))
                continue
            elif prompt.startswith('/'):
                print_formatted_text(HTML(f"<ansired>Unknown command: {prompt}</ansired>"))
                print_formatted_text(HTML("Available commands: /verbose on, /verbose off"))
                continue

            return prompt

    def run_main_context(self):
        """Run one iteration of the main context."""
        try:
            print_formatted_text(HTML("<ansicyan>Main context thinking...</ansicyan>"))

            response = self.main_context.call_llm(logger=self.logger, verbose=self.verbose)
            tool_call = response['tool_call']
            usage = response['usage']

            # Log the LLM call
            tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
            cost = usage.get('cost', 0)
            self.logger.log_llm_call(self.main_context.model, tokens, cost, 'main')

            # Log tool call
            tool_name = tool_call['function']['name']
            arguments = json.loads(tool_call['function']['arguments'])
            self.logger.log_tool_call(tool_name, arguments, 'main')

            print_formatted_text(HTML(
                f"<ansigreen>→</ansigreen> Tool: <ansiyellow>{tool_name}</ansiyellow>"
            ))

            return tool_call

        except Exception as e:
            self.logger.log_error(str(e), 'main')
            raise

    def execute_main_tool(self, tool_call: Dict) -> tuple:
        """
        Execute a main context tool call.

        Returns:
            Tuple of (result_string, should_run_subcontext, subcontext_name)
        """
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        if tool_name == 'get_user_input':
            result = tools.get_user_input(
                arguments['prompt'],
                arguments.get('preset_answers')
            )
            self.logger.log_tool_result(tool_name, result, 0, 'main')
            return (result, False, None)

        elif tool_name == 'create_subcontext':
            context_type = arguments['context_type']
            task = arguments['task']
            model = arguments.get('model', 'auto')

            # Auto-generate unique name
            if context_type not in self.context_counters:
                self.context_counters[context_type] = 0
            self.context_counters[context_type] += 1
            unique_name = f"{context_type}{self.context_counters[context_type]}"

            # Create the subcontext
            subcontext = contexts.create_context(
                unique_name, context_type, model,
                worktree_path=self.worktree_path
            )
            self.subcontexts[unique_name] = subcontext

            # Add the task to the subcontext
            subcontext.add_message('user', task)

            result = f"Created {context_type} subcontext '{unique_name}'"
            self.logger.log_tool_result(tool_name, result, 0, 'main')

            print_formatted_text(HTML(
                f"  <ansigreen>Created subcontext:</ansigreen> {unique_name} ({context_type})"
            ))

            return (result, True, unique_name)

        elif tool_name == 'run_oneshot_per_file':
            path_regex = arguments['path_regex']
            task = arguments['task']
            file_limit = arguments.get('file_limit', 5)
            model = arguments.get('model', 'auto')

            # Auto-generate base name for file_processor contexts
            context_type = 'file_processor'
            if context_type not in self.context_counters:
                self.context_counters[context_type] = 0
            self.context_counters[context_type] += 1
            base_name = f"file_processor{self.context_counters[context_type]}"

            # Find matching files
            import re
            from pathlib import Path as PathLib

            pattern = re.compile(path_regex)
            matching_files = []

            for root, dirs, files in os.walk(self.worktree_path):
                # Skip .git, .scratch, .maca
                dirs[:] = [d for d in dirs if not d.startswith('.')]

                for file in files:
                    full_path = PathLib(root) / file
                    rel_path = full_path.relative_to(self.worktree_path)
                    rel_path_str = str(rel_path)

                    if pattern.search(rel_path_str):
                        matching_files.append(rel_path_str)

            # Check file limit
            if len(matching_files) > file_limit:
                error_msg = f"Matched {len(matching_files)} files, exceeds limit of {file_limit}. Use list_files(r'{path_regex}') first to see what matches, then adjust regex or increase file_limit."
                self.logger.log_tool_result(tool_name, error_msg, 0, 'main')
                print_formatted_text(HTML(f"  <ansired>Error:</ansired> {error_msg}"))
                return (error_msg, False, None)

            # Create one subcontext per file
            results = []
            for file_path in matching_files:
                file_unique_name = f"{base_name}-{file_path.replace('/', '-').replace('\\', '-')}"

                # Read file contents
                full_file_path = self.worktree_path / file_path
                try:
                    file_contents = full_file_path.read_text()
                except Exception as e:
                    results.append(f"{file_path}: Error reading - {e}")
                    continue

                # Create subcontext
                subcontext = contexts.create_context(
                    file_unique_name, context_type, model,
                    worktree_path=self.worktree_path
                )
                self.subcontexts[file_unique_name] = subcontext

                # Add file contents and task to context
                file_message = f"File: {file_path}\n\n```\n{file_contents}\n```\n\n{task}"
                subcontext.add_message('user', file_message)

                results.append(f"{file_path} -> {file_unique_name}")

            result = f"Created {len(matching_files)} file_processor subcontexts:\n" + "\n".join(results)
            self.logger.log_tool_result(tool_name, result, 0, 'main')

            print_formatted_text(HTML(
                f"  <ansigreen>Created {len(matching_files)} file_processor subcontexts</ansigreen>"
            ))

            # Return first subcontext name to run
            if matching_files:
                first_name = f"{base_name}-{matching_files[0].replace('/', '-').replace('\\', '-')}"
                return (result, True, first_name)
            else:
                return (result, False, None)

        elif tool_name == 'continue_subcontext':
            unique_name = arguments['unique_name']
            guidance = arguments.get('guidance', '')

            if unique_name not in self.subcontexts:
                raise ValueError(f"Unknown subcontext: {unique_name}")

            # Add guidance if provided
            if guidance:
                self.subcontexts[unique_name].add_message('user', guidance)

            result = f"Continuing subcontext '{unique_name}'"
            self.logger.log_tool_result(tool_name, result, 0, 'main')

            print_formatted_text(HTML(
                f"  <ansigreen>Continuing subcontext:</ansigreen> {unique_name}"
            ))

            return (result, True, unique_name)

        elif tool_name == 'complete':
            return ('COMPLETE', False, None)

        else:
            raise ValueError(f"Unknown main tool: {tool_name}")

    def run_subcontext(self, unique_name: str):
        """Run one iteration of a subcontext."""
        subcontext = self.subcontexts[unique_name]

        try:
            print_formatted_text(HTML(
                f"<ansicyan>  Subcontext '{unique_name}' thinking...</ansicyan>"
            ))

            start_time = time.time()
            response = subcontext.call_llm(logger=self.logger, verbose=self.verbose)
            tool_call = response['tool_call']
            usage = response['usage']

            # Log LLM call
            tokens = usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)
            cost = usage.get('cost', 0)
            self.logger.log_llm_call(subcontext.model, tokens, cost, unique_name)

            # Execute the tool
            tool_name = tool_call['function']['name']
            arguments = json.loads(tool_call['function']['arguments'])

            self.logger.log_tool_call(tool_name, arguments, unique_name)

            print_formatted_text(HTML(
                f"  <ansigreen>→</ansigreen> Tool: <ansiyellow>{tool_name}</ansiyellow>"
            ))
            if 'rationale' in arguments:
                print_formatted_text(HTML(f"    Rationale: {arguments['rationale']}"))

            # Execute tool
            tool_start = time.time()

            if tool_name == 'complete':
                result = arguments.get('result', 'Task completed')
                tool_duration = 0
            else:
                result = subcontext.execute_tool(tool_call)
                tool_duration = time.time() - tool_start

            self.logger.log_tool_result(tool_name, result, tool_duration, unique_name)

            # Add tool result to subcontext
            subcontext.add_tool_result(tool_call, result)

            # Check for git changes and commit if needed
            diff_stats = git_ops.get_diff_stats(self.worktree_path)

            agents_md_updated = False
            if diff_stats:
                # Commit changes
                commit_msg = arguments.get('rationale', f'{tool_name} executed')
                git_ops.commit_changes(self.worktree_path, commit_msg)
                self.logger.log_commit(commit_msg, diff_stats, unique_name)

                print_formatted_text(HTML(
                    f"    <ansigreen>✓ Committed changes</ansigreen>"
                ))

                # Check if AGENTS.md was updated and refresh all contexts
                if self.main_context.update_agents_md():
                    agents_md_updated = True
                    print_formatted_text(HTML(
                        f"    <ansicyan>AGENTS.md updated - refreshed all contexts</ansicyan>"
                    ))
                    # Update all subcontexts too
                    for ctx in self.subcontexts.values():
                        ctx.update_agents_md()

            # Build summary for main context
            summary = self._build_subcontext_summary(
                unique_name, tool_name, arguments.get('rationale', ''),
                tokens, cost, tool_duration, diff_stats, result
            )

            # If this was an implementation context and AGENTS.md was updated, ask main to verify
            if subcontext.context_type == 'implementation' and agents_md_updated:
                summary += ("\n\nNote: AGENTS.md was updated by this implementation. Please verify from "
                           "a high-level perspective whether this update was appropriate and necessary. "
                           "You may query the subcontext for the rationale behind the updates if needed.")

            # Add summary to main context
            self.main_context.add_message('user', summary)
            self.logger.log_subcontext_summary({
                'tool': tool_name,
                'tokens': tokens,
                'cost': cost,
                'duration': tool_duration,
                'diff_stats': diff_stats,
                'agents_md_updated': agents_md_updated
            }, unique_name)

        except Exception as e:
            self.logger.log_error(str(e), unique_name)
            error_msg = f"Error in subcontext '{unique_name}': {str(e)}"
            self.main_context.add_message('user', error_msg)
            print_formatted_text(HTML(f"<ansired>{error_msg}</ansired>"))

    def _build_subcontext_summary(
        self, unique_name: str, tool_name: str, rationale: str,
        tokens: int, cost: float, duration: float, diff_stats: str, result: any
    ) -> str:
        """Build a summary message to send back to main context."""
        summary = f"Subcontext '{unique_name}' executed tool: {tool_name}\n\n"
        summary += f"Rationale: {rationale}\n\n"
        summary += f"Tokens used: {tokens}, Cost: ${cost:.6f}, Duration: {duration:.2f}s\n\n"

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
        print_formatted_text(HTML(f"\n<ansigreen>Task completed!</ansigreen>\n{result}\n"))

        # Show summary
        stats = self.logger.get_stats()
        print_formatted_text(HTML(
            f"<ansicyan>Session stats:</ansicyan> "
            f"{stats['total_tokens']} tokens, "
            f"${stats['total_cost']:.6f} cost"
        ))

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
        print_formatted_text(HTML("<ansicyan>Merging changes...</ansicyan>"))

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
            print_formatted_text(HTML(f"<ansired>Merge failed: {message}</ansired>"))
            print("You may need to resolve conflicts manually or spawn a merge context.")
            # TODO: Spawn merge context here
            sys.exit(1)

        # Cleanup
        git_ops.cleanup_session(self.repo_root, self.worktree_path, self.branch_name)

        print_formatted_text(HTML("<ansigreen>✓ Merged and cleaned up</ansigreen>"))

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
        self.main_context = contexts.MainContext(worktree_path=self.worktree_path)

        # Auto-call list_files for top-level directory to give context about project structure
        try:
            top_files_result = tools.execute_tool('list_files', {'path_regex': r'^[^/\\]*$'}, context_type='main')
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

            self.logger.log_message('user', prompt, 'main')
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
