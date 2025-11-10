#!/usr/bin/env python3
"""Session logging in human-readable format for audit and future resumption."""

import random
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


class SessionLogger:
    """Logs session events to per-context log files in human-readable format."""

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
        self.seq = 0  # Global sequence number across all log entries

        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _find_heredoc_delimiter(self, value: str) -> str:
        """Find a delimiter that doesn't appear in the value."""
        delimiter = 'EOD'
        if f'\n{delimiter}\n' not in value:
            return delimiter
        
        # Try random delimiters until we find one that doesn't appear
        for _ in range(100):
            delimiter = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            if f'\n{delimiter}\n' not in value:
                return delimiter
        
        # Fallback to a very long random string
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=32))

    def _format_value(self, value: Any) -> tuple[str, str]:
        """
        Format a value for logging, using HEREDOC for multiline or <<< prefixed strings.
        
        Returns:
            Tuple of (formatted_value, key_suffix) where key_suffix is '!' for non-strings, '' otherwise
        """
        import json
        
        # Handle non-string types by encoding as JSON
        if not isinstance(value, str):
            if value is None:
                return ('', '')
            return (json.dumps(value), '!')
        
        # For strings, use HEREDOC for multiline strings or strings starting with <<<
        if '\n' in value or value.startswith('<<<'):
            delimiter = self._find_heredoc_delimiter(value)
            return (f'<<<{delimiter}\n{value}\n{delimiter}', '')
        
        return (value, '')

    def log(self, context_id: Optional[str] = None, **kwargs):
        """
        Log an entry to the appropriate context log file.

        Args:
            context_id: Optional context identifier (main or subcontext name)
            **kwargs: Arbitrary key-value pairs to log
        """
        context_id = context_id or 'main'
        log_path = self.session_dir / f'{context_id}.log'

        # Increment global sequence number
        self.seq += 1

        # Format timestamp in human-readable format
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Build the log entry
        lines = []
        lines.append(f'timestamp: {timestamp}')
        lines.append(f'seq!: {self.seq}')
        
        # Add all kwargs as key-value pairs
        for key, value in kwargs.items():
            formatted_value, suffix = self._format_value(value)
            lines.append(f'{key}{suffix}: {formatted_value}')

        with open(log_path, 'a') as f:
            f.write('\n'.join(lines))
            f.write('\n\n')  # Add blank line separator between log entries

    def read_log(self, context_id: str = 'main') -> list:
        """
        Read all entries from a context log file.

        Args:
            context_id: The context whose log to read

        Returns:
            List of log entries as dictionaries
        """
        import json
        
        log_path = self.session_dir / f'{context_id}.log'
        entries = []
        
        if not log_path.exists():
            return entries
        
        with open(log_path, 'r') as f:
            content = f.read()
        
        # Parse entries separated by blank lines
        current_entry = {}
        lines = content.split('\n')
        i = 0
        
        while i < len(lines):
            line = lines[i]
            
            if not line.strip():
                # Blank line - end of entry
                if current_entry:
                    entries.append(current_entry)
                    current_entry = {}
                i += 1
                continue
            
            # Check for HEREDOC
            if ': <<<' in line:
                # Extract key (with potential ! suffix)
                key_part, rest = line.split(': <<<', 1)
                delimiter = rest.strip()
                
                # Check if key has ! suffix (JSON-encoded value)
                if key_part.endswith('!'):
                    key = key_part[:-1]
                    is_json = True
                else:
                    key = key_part
                    is_json = False
                
                # Find the end delimiter
                heredoc_lines = []
                i += 1
                while i < len(lines) and lines[i] != delimiter:
                    heredoc_lines.append(lines[i])
                    i += 1
                
                value = '\n'.join(heredoc_lines)
                current_entry[key] = json.loads(value) if is_json else value
                i += 1
            elif ': ' in line:
                # Simple key-value
                key_part, value = line.split(': ', 1)
                
                # Check if key has ! suffix (JSON-encoded value)
                if key_part.endswith('!'):
                    key = key_part[:-1]
                    current_entry[key] = json.loads(value)
                else:
                    key = key_part
                    current_entry[key] = value
                i += 1
            else:
                i += 1
        
        # Don't forget the last entry if file doesn't end with blank line
        if current_entry:
            entries.append(current_entry)

        return entries
