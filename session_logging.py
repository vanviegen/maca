#!/usr/bin/env python3
"""Session logging in JSONL format for audit and future resumption."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SessionLogger:
    """Logs session events to a JSONL file."""

    def __init__(self, repo_root: Path, session_id: int):
        """
        Initialize the session logger.

        Args:
            repo_root: Path to the repository root
            session_id: The session ID number
        """
        self.repo_root = repo_root
        self.session_id = session_id
        self.log_path = repo_root / '.aai' / f'{session_id}.log'
        self.total_cost = 0.0
        self.total_tokens = 0

        # Ensure .aai directory exists
        self.log_path.parent.mkdir(exist_ok=True)

        # Create/open log file
        if not self.log_path.exists():
            self.log_path.touch()

    def log(self, entry_type: str, data: Dict[str, Any], context_id: Optional[str] = None):
        """
        Log an entry to the session log.

        Args:
            entry_type: Type of entry (message, tool_call, tool_result, commit, summary, etc.)
            data: The data to log
            context_id: Optional context identifier (main or subcontext name)
        """
        entry = {
            'type': entry_type,
            'timestamp': time.time(),
            'context_id': context_id or 'main',
            'data': data
        }

        with open(self.log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def log_message(self, role: str, content: str, context_id: Optional[str] = None):
        """Log a message (user or assistant)."""
        self.log('message', {
            'role': role,
            'content': content
        }, context_id)

    def log_tool_call(self, tool_name: str, arguments: Dict[str, Any], context_id: Optional[str] = None):
        """Log a tool call."""
        self.log('tool_call', {
            'tool_name': tool_name,
            'arguments': arguments
        }, context_id)

    def log_tool_result(self, tool_name: str, result: Any, duration: float, context_id: Optional[str] = None):
        """Log a tool execution result."""
        self.log('tool_result', {
            'tool_name': tool_name,
            'result': str(result)[:1000],  # Truncate long results
            'duration': duration
        }, context_id)

    def log_commit(self, message: str, diff_stats: str, context_id: Optional[str] = None):
        """Log a git commit."""
        self.log('commit', {
            'message': message,
            'diff_stats': diff_stats
        }, context_id)

    def log_llm_call(self, model: str, tokens: int, cost: float, context_id: Optional[str] = None):
        """Log an LLM API call with usage statistics."""
        self.total_tokens += tokens
        self.total_cost += cost

        self.log('llm_call', {
            'model': model,
            'tokens': tokens,
            'cost': cost,
            'total_tokens': self.total_tokens,
            'total_cost': self.total_cost
        }, context_id)

    def log_subcontext_summary(self, summary: Dict[str, Any], context_id: str):
        """Log a summary sent back to main context after subcontext tool execution."""
        self.log('subcontext_summary', summary, context_id)

    def log_error(self, error: str, context_id: Optional[str] = None):
        """Log an error."""
        self.log('error', {'error': error}, context_id)

    def get_stats(self) -> Dict[str, Any]:
        """Get current session statistics."""
        return {
            'session_id': self.session_id,
            'total_tokens': self.total_tokens,
            'total_cost': self.total_cost,
            'total_cost_millis': self.total_cost * 1000
        }

    def read_log(self) -> list:
        """Read all entries from the log file."""
        entries = []
        if self.log_path.exists():
            with open(self.log_path, 'r') as f:
                for line in f:
                    if line.strip():
                        entries.append(json.loads(line))
        return entries
