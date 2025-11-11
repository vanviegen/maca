#!/usr/bin/env python3
import json        
import random
import string
from datetime import datetime
from pathlib import Path


class Logger:
    """Logs session events to per-context log files in human-readable format."""
    
    # Global sequence number across all log entries
    seq = 0

    def __init__(self, repo_root: Path, session_id: int, context_id: str):
        """
        Initialize the session logger.

        Args:
            repo_root: Path to the repository root
            session_id: The session ID number
        """
        session_dir = repo_root / '.maca' / str(session_id)

        # Ensure session directory exists
        session_dir.mkdir(parents=True, exist_ok=True)

        self.file = open(session_dir / f"{context_id}.log", 'a')

    def _find_heredoc_delimiter(self, value: str) -> str:
        """Find a delimiter that doesn't appear in the value."""
        # Simple approach: use "EOD" unless it appears in the value
        if f'\nEOD' not in value:
            return "EOD"
        
        # Just return a random string - chances of collision are infinitesimal
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=10))

    def log(self, **kwargs):
        """
        Log an entry to the appropriate context log file.

        Args:
            context_id: Optional context identifier (main or subcontext name)
            **kwargs: Arbitrary key-value pairs to log
        """
        # Increment global sequence number
        Logger.seq += 1

        # Format timestamp in human-readable format
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Build the log entry
        lines = []
        lines.append(f'timestamp: {timestamp}')
        lines.append(f'seq!: {Logger.seq}')
        
        # Add all kwargs as key-value pairs
        for key, value in kwargs.items():
            # Handle non-string types by encoding as JSON
            if not isinstance(value, str):
                key += '!'
                value = json.dumps(value)
            else:
                value = value.strip()
                if '\n' in value or value.startswith('<<<'):
                    delimiter = self._find_heredoc_delimiter(value)
                    value = f'<<<{delimiter}\n{value}\n{delimiter}'
            lines.append(f'{key}: {value}')

        self.file.write('\n'.join(lines) + '\n\n')

    @staticmethod
    def read_log(repo_root: Path, session_id: int, context_id: str) -> list:
        log_path = repo_root / '.maca' / str(session_id) / f"{context_id}.log"
        if not log_path.exists():
            return False
        
        # Parse entries separated by blank lines
        current_entry = {}
        delimiter = key = value = None

        with open(log_path, 'r') as f:
            for line in f:
                if delimiter:
                    # Inside HEREDOC
                    if line.rstrip() != delimiter:
                        value += line
                        continue

                    # End of HEREDOC
                    current_entry[key] = json.loads(value) if is_json else value
                    delimiter = None
                else:
                    line = line.rstrip()
                    if not line:
                        # Blank line - end of entry
                        if current_entry:
                            yield current_entry
                            current_entry = {}
                        continue

                    s = line.split(': ', 1)
                    if len(s) != 2:
                        raise ValueError(f"Malformed log line: {line}")
                    key, value = s
                
                    # Check for HEREDOC
                    if value.startswith('<<<'):
                        # Extract key (with potential ! suffix)
                        delimiter = value[3:]
                        value = ''
                        continue
                    
                # Check if key has ! suffix (JSON-encoded value)
                if key.endswith('!'):
                    key = key[:-1]
                    value = json.loads(value)
                
                current_entry[key] = value
            
        # Don't forget the last entry if file doesn't end with blank line
        if current_entry:
            yield current_entry

        return True
