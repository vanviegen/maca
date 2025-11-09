#!/usr/bin/env python3
"""Session logging in JSONL format for audit and future resumption."""

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional


class SessionLogger:
    """Logs session events to per-context JSONL files."""

    def __init__(self, repo_root: Path, session_id: int):
        """
        Initialize the session logger.

        Args:
            repo_root: Path to the repository root
            session_id: The session ID number
        """
        self.repo_root = repo_root
        self.session_id = session_id
        self.session_dir = repo_root / '.maca' / str(session_id)
        self.total_cost = 0.0
        self.total_tokens = 0
        self.seq = 0  # Global sequence number across all log entries

        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def log(self, entry_type: str, data: Dict[str, Any], context_id: Optional[str] = None):
        """
        Log an entry to the appropriate context log file.

        Args:
            entry_type: Type of entry (message, tool_call, tool_result, commit, summary, etc.)
            data: The data to log
            context_id: Optional context identifier (main or subcontext name)
        """
        context_id = context_id or 'main'
        log_path = self.session_dir / f'{context_id}.log'

        # Increment global sequence number
        self.seq += 1

        entry = {
            'seq': self.seq,
            'type': entry_type,
            'timestamp': time.time(),
            'data': data
        }

        with open(log_path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

    def log_message(self, role: str, content: str, context_id: Optional[str] = None):
        """Log a message (user or assistant)."""
        self.log('message', {
            'role': role,
            'content': content
        }, context_id)

    def log_full_prompt(self, messages: list, context_id: Optional[str] = None):
        """Log the full prompt sent to the LLM."""
        self.log('full_prompt', {
            'messages': messages
        }, context_id)

    def log_full_response(self, response: dict, context_id: Optional[str] = None):
        """Log the full response from the LLM."""
        self.log('full_response', response, context_id)

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

    def read_log(self, context_id: str = 'main') -> list:
        """
        Read all entries from a context log file.

        Args:
            context_id: The context whose log to read

        Returns:
            List of log entries
        """
        log_path = self.session_dir / f'{context_id}.log'
        entries = []
        if log_path.exists():
            with open(log_path, 'r') as f:
                for line in f:
                    if line.strip():
                        entries.append(json.loads(line))
        return entries
