#!/usr/bin/env python3
"""Command parser for text-based MACA commands."""

import re
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class Command:
    """Represents a parsed command."""
    id: int
    command: str
    args: Dict[str, str]
    line_number: int  # For error reporting


@dataclass
class ParseResult:
    """Result of parsing LLM output."""
    commands: List[Command]
    thinking: str  # All non-command text (for display/logging)


def parse_commands(text: str) -> ParseResult:
    """
    Parse commands from LLM text output.

    Format:
        ~maca~ ID COMMAND
        arg_name: arg_value
        another_arg: value

        More thinking text here...

        ~maca~ ID2 COMMAND2
        arg: ~maca~start~
        multi-line
        content here
        ~maca~end~

    Args:
        text: Raw text from LLM

    Returns:
        ParseResult with commands and thinking text
    """
    lines = text.split('\n')
    commands = []
    thinking_lines = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for command marker at start of line
        if line.startswith('~maca~ '):
            # Parse command header
            header = line[7:].strip()  # Remove '~maca~ '
            parts = header.split(None, 1)  # Split on first whitespace

            if len(parts) != 2:
                # Malformed command - treat as thinking
                thinking_lines.append(line)
                i += 1
                continue

            try:
                cmd_id = int(parts[0])
            except ValueError:
                # Invalid ID - treat as thinking
                thinking_lines.append(line)
                i += 1
                continue

            cmd_name = parts[1].strip()
            cmd_args = {}
            cmd_line = i
            i += 1

            # Parse arguments until blank line or next command
            while i < len(lines):
                arg_line = lines[i]

                # Blank line or next command ends this command
                if not arg_line.strip() or arg_line.startswith('~maca~ '):
                    break

                # Parse argument
                if ':' not in arg_line:
                    # Malformed argument line - skip it
                    i += 1
                    continue

                arg_name, arg_value = arg_line.split(':', 1)
                arg_name = arg_name.strip()
                arg_value = arg_value.strip()

                # Check for multi-line content
                if arg_value == '~maca~start~':
                    # Collect multi-line content
                    content_lines = []
                    i += 1

                    while i < len(lines):
                        content_line = lines[i]

                        # Check for end marker (possibly escaped)
                        if re.match(r'^~+maca~end~$', content_line):
                            # Unescape if needed
                            if content_line.startswith('~~'):
                                # This was an escaped end marker - remove one ~
                                content_lines.append(content_line[1:])
                                i += 1
                            else:
                                # Real end marker
                                i += 1
                                break
                        else:
                            content_lines.append(content_line)
                            i += 1

                    arg_value = '\n'.join(content_lines)

                cmd_args[arg_name] = arg_value
                i += 1

            # Store command
            commands.append(Command(
                id=cmd_id,
                command=cmd_name,
                args=cmd_args,
                line_number=cmd_line + 1  # 1-indexed
            ))
        else:
            # Not a command - it's thinking text
            thinking_lines.append(line)
            i += 1

    thinking = '\n'.join(thinking_lines).strip()

    return ParseResult(commands=commands, thinking=thinking)


def format_command_results(results: List[Dict], long_term: bool = False) -> str:
    """
    Format command results back to the LLM.

    Args:
        results: List of result dicts (one per command)
        long_term: If True, omit large data (for long-term context)

    Returns:
        Formatted text
    """
    lines = []

    for result in results:
        cmd_id = result['id']
        status = result.get('status', 'success')

        # Start command result
        lines.append(f"~maca~ {cmd_id}")
        lines.append(f"status: {status}")

        # Add result fields (omitting large data if long_term)
        for key, value in result.items():
            if key in ('id', 'status'):
                continue

            # For long-term context, omit large data
            if long_term and key in ('content', 'data', 'output', 'matches', 'results'):
                lines.append(f"{key}: OMITTED")
            else:
                # Format value
                if isinstance(value, str) and '\n' in value:
                    # Multi-line value
                    lines.append(f"{key}: ~maca~start~")
                    # Escape lines that look like end markers
                    for line in value.split('\n'):
                        if re.match(r'^~+maca~end~$', line):
                            lines.append('~' + line)
                        else:
                            lines.append(line)
                    lines.append("~maca~end~")
                elif isinstance(value, (list, dict)):
                    # Complex value - use repr
                    lines.append(f"{key}: {repr(value)}")
                else:
                    lines.append(f"{key}: {value}")

        lines.append('')  # Blank line after command

    return '\n'.join(lines)


def get_cancelled_ids(commands: List[Command]) -> set:
    """
    Extract IDs of cancelled commands.

    Args:
        commands: Parsed commands

    Returns:
        Set of command IDs that were cancelled
    """
    cancelled = set()

    for cmd in commands:
        if cmd.command == 'CANCEL':
            cancel_id = cmd.args.get('id')
            if cancel_id:
                try:
                    cancelled.add(int(cancel_id))
                except ValueError:
                    pass

    return cancelled
