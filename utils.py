"""Utility functions for MACA."""

from dataclasses import dataclass
from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import FormattedText
from pathlib import Path
from fnmatch import fnmatch
from typing import List, Dict, Any, Optional, Union
import json
import urllib.request
import time
from logger import log
import re


# Global cumulative cost tracking
_cumulative_cost = 0


@dataclass(frozen=True)
class Color:
    """Color constant for cprint."""
    code: str


# Color constants for log messages
C_GOOD = Color('#2ecc71')      # Modern green (emerald)
C_BAD = Color('#e74c3c')       # Modern red (alizarin)
C_NORMAL = Color('')           # White/default
C_IMPORTANT = Color('#f39c12') # Modern orange (orange)
C_INFO = Color('#3498db')      # Modern blue (peter river)


def cprint(*args):
    """
    Print with color constants and text where color persists until changed.
    
    Args:
        *args: Mixed arguments - Color instances change the current color,
               all other values are printed as text with the current color.
    
    Example:
        cprint(C_BAD, "Error: ", C_IMPORTANT, msg, C_NORMAL, " attempt ", attempt)
        # "Error: " in red, msg in orange, " attempt " and attempt in default color
    """
    formatted_parts = []
    current_color = ''
    
    for arg in args:
        # Check if this is a Color instance
        if isinstance(arg, Color):
            # It's a color, update current color
            current_color = arg.code
        else:
            # It's text, print with current color
            formatted_parts.append((current_color, str(arg)))
    
    print_formatted_text(FormattedText(formatted_parts))


class GitignoreMatcher:
    """Matcher for gitignore-style patterns."""
    
    def __init__(self, patterns: List[str]):
        """
        Initialize with gitignore patterns.
        
        Args:
            patterns: List of gitignore patterns
        """
        self.patterns = []
        for pattern in patterns:
            pattern = pattern.strip()
            if not pattern or pattern.startswith('#'):
                continue
            
            # Track if this is a negation pattern
            negation = pattern.startswith('!')
            if negation:
                pattern = pattern[1:]
            
            # Track if this is a directory-only pattern
            dir_only = pattern.endswith('/')
            if dir_only:
                pattern = pattern[:-1]
            
            self.patterns.append({
                'pattern': pattern,
                'negation': negation,
                'dir_only': dir_only
            })
    
    def matches(self, path: str, is_dir: bool = False) -> bool:
        """
        Check if a path matches any gitignore pattern.
        
        Args:
            path: Path to check (relative to gitignore location)
            is_dir: Whether the path is a directory
            
        Returns:
            True if path should be ignored
        """
        # Process patterns in order, last match wins
        ignored = False
        
        for p in self.patterns:
            pattern = p['pattern']
            
            # Skip directory-only patterns for files
            if p['dir_only'] and not is_dir:
                continue
            
            # Match against the pattern
            matched = False
            
            # If pattern contains '/', it's relative to the gitignore location
            if '/' in pattern:
                # Full path match
                matched = fnmatch(path, pattern)
            else:
                # Match against any path component
                matched = fnmatch(path, pattern) or fnmatch(path, f'**/{pattern}')
                # Also check if any component matches
                parts = Path(path).parts
                for part in parts:
                    if fnmatch(part, pattern):
                        matched = True
                        break
            
            if matched:
                # If negation, unignore; otherwise ignore
                ignored = not p['negation']
        
        return ignored


def parse_gitignore(gitignore_path: Path) -> GitignoreMatcher:
    """
    Parse a .gitignore file and return a matcher.
    
    Args:
        gitignore_path: Path to .gitignore file
        
    Returns:
        GitignoreMatcher instance
    """
    if not gitignore_path.exists():
        return GitignoreMatcher([])
    
    try:
        patterns = gitignore_path.read_text().splitlines()
        return GitignoreMatcher(patterns)
    except Exception:
        return GitignoreMatcher([])


def call_llm(
    api_key: str,
    model: str,
    messages: List[Dict[str, Any]],
    tool_schemas: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Call the OpenRouter LLM API with retry logic.
    
    Args:
        api_key: OpenRouter API key
        model: Model identifier (e.g., "anthropic/claude-sonnet-4.5")
        messages: List of message dicts with role and content
        tool_schemas: List of tool schemas
    
    Returns:
        Dict with:
        - message: Assistant message dict
        - cost: Cost in microdollars (integer)
        - usage: Usage dict with token counts
    
    Raises:
        Exception: If API call fails after 3 retries
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'HTTP-Referer': 'https://github.com/vanviegen/maca',
        'X-Title': 'MACA - Multi-Agent Coding Assistant'
    }

    data = {
        'model': model,
        'messages': messages,
        'tools': tool_schemas,
        'usage': {"include": True},
        'tool_choice': 'required',
        # 'reasoning': {
        #     'effort': 'medium'
        # }
    }
    
    # Retry up to 3 times
    last_error = None
    for retry in range(3):
        start_time = time.time()

        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(data).encode('utf-8'),
                headers=headers
            )
            with urllib.request.urlopen(req) as response:
                result = json.loads(response.read().decode('utf-8'))
            
            # Extract response
            choice = result['choices'][0]
            message = choice['message']

            # Extract usage
            usage = result.get('usage', {})
            cost = int(usage.get('cost', 0) * 1_000_000)  # Convert dollars to microdollars
            duration = time.time() - start_time

            # Update global cumulative cost
            global _cumulative_cost
            _cumulative_cost += cost

            # Log the call
            log(tag='llm_call', model=model, cost=cost, prompt_tokens=usage['prompt_tokens'], completion_tokens=usage['completion_tokens'], duration=duration)

            return {
                'message': message,
                'cost': cost,
                'usage': usage
            }
            
        except Exception as e:
            last_error = e
            if hasattr(e, 'read'):
                error_body = e.read().decode('utf-8')
            else:
                error_body = str(e)
            
            if retry < 2:  # Don't log on the last retry
                cprint(C_BAD, f"LLM error: {error_body}. Attempt {retry+1}/3.")
                log(tag='error', error="LLM ERROR", retry=retry, message=str(error_body))
    
    # All retries failed
    raise Exception(f"LLM call failed after 3 retries: {last_error}")


def get_cumulative_cost() -> int:
    """
    Get the cumulative cost of all LLM calls in microdollars.
    
    Returns:
        Cumulative cost in microdollars
    """
    return _cumulative_cost


def compute_diff(old_text: str, new_text: str) -> Optional[str]:
    """
    Compute a simple unified diff between old and new text.
    
    Returns None if texts are identical.
    """
    if old_text == new_text:
        return None
    
    import difflib
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, lineterm='')
    diff_text = ''.join(diff)
    
    return diff_text if diff_text else None


def get_matching_files(
    worktree_path: Path,
    include: Optional[Union[str, List[str]]] = "**",
    exclude: Optional[Union[str, List[str]]] = ".*",
    exclude_files: Optional[Union[str, List[str]]] = None
) -> List[Path]:
    """
    Get list of files matching include/exclude glob patterns.

    Args:
        worktree_path: Path to the worktree
        include: Glob pattern(s) to include. Can be None, a string, or list of strings.
                 Defaults to "**" (all files).
        exclude: Glob pattern(s) to exclude. Can be None, a string, or list of strings.
                 Defaults to ".*" (hidden files/directories).
        exclude_files: File(s) containing exclude patterns (e.g., ".gitignore"). Can be None, a string, or list of strings.
                       Defaults to None. When ".gitignore" is included, gitignore semantics are applied.

    Returns:
        List of Path objects for matching files (not directories)
    """
    from utils import parse_gitignore
    
    worktree = Path(worktree_path)

    # Normalize include patterns
    if include is None:
        include_patterns = ["**"]
    elif isinstance(include, str):
        include_patterns = [include]
    else:
        include_patterns = include

    # Normalize exclude patterns
    if exclude is None:
        exclude_patterns = []
    elif isinstance(exclude, str):
        exclude_patterns = [exclude]
    else:
        exclude_patterns = exclude

    # Normalize exclude_files
    if exclude_files is None:
        exclude_file_list = []
    elif isinstance(exclude_files, str):
        exclude_file_list = [exclude_files]
    else:
        exclude_file_list = exclude_files

    # Parse .gitignore if present in exclude_files
    gitignore_matcher = None
    if '.gitignore' in exclude_file_list:
        gitignore_path = worktree / '.gitignore'
        gitignore_matcher = parse_gitignore(gitignore_path)

    # Collect all matching files
    matching_files = set()

    for pattern in include_patterns:
        for path in worktree.glob(pattern):
            if path.is_file():
                matching_files.add(path)

    # Filter out excluded files
    filtered_files = []
    for file_path in matching_files:
        rel_path_str = str(file_path.relative_to(worktree))

        # Check gitignore first
        if gitignore_matcher:
            if gitignore_matcher.matches(rel_path_str, is_dir=False):
                continue

        # Check if any exclude pattern matches
        excluded = False
        for exc_pattern in exclude_patterns:
            # Check if pattern matches any part of the path
            if fnmatch(rel_path_str, exc_pattern):
                excluded = True
                break
            # Also check individual path components
            for part in Path(rel_path_str).parts:
                if fnmatch(part, exc_pattern):
                    excluded = True
                    break
            if excluded:
                break

        if not excluded:
            filtered_files.append(file_path)

    return sorted(filtered_files)


def find_json_truncation_point(json):
    path, i = [], 0
    
    while i < len(json) and json[i].isspace():
        i += 1
    
    if i >= len(json):
        return path
    
    stack = []
    
    while i < len(json):
        while i < len(json) and json[i].isspace():
            i += 1
        if i >= len(json):
            break
            
        c = json[i]
        
        if c == '"':
            start = i
            i += 1
            while i < len(json) and json[i] != '"':
                i += 2 if json[i] == '\\' else 1
            if i >= len(json):
                break
            i += 1
            
            # Check if this is a key
            j = i
            while j < len(json) and json[j].isspace():
                j += 1
            if j < len(json) and json[j] == ':':
                key = json[start+1:i-1].replace('\\"', '"').replace('\\\\', '\\')
                i = j + 1
                stack.append(('obj', key))
            elif stack and stack[-1][0] == 'obj':
                stack.pop()
                
        elif c == '{':
            i += 1
        elif c == '[':
            stack.append(('arr', 0))
            i += 1
        elif c == ']':
            if stack and stack[-1][0] == 'arr':
                stack.pop()
            i += 1
        elif c == '}':
            if stack and stack[-1][0] == 'obj':
                stack.pop()
            i += 1
        elif c == ',':
            if stack and stack[-1][0] == 'arr':
                stack[-1] = ('arr', stack[-1][1] + 1)
            elif stack and stack[-1][0] == 'obj':
                stack.pop()
            i += 1
        elif re.match(r'[-0-9]', c):
            m = re.match(r'-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?', json[i:])
            i += len(m.group(0)) if m else 1
            if stack and stack[-1][0] == 'obj':
                stack.pop()
        elif json[i:i+4] in ('true', 'null') or json[i:i+5] == 'false':
            i += 4 if json[i] == 't' or json[i] == 'n' else 5
            if stack and stack[-1][0] == 'obj':
                stack.pop()
        else:
            i += 1
    
    return [x[1] for x in stack]
