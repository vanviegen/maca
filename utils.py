"""Utility functions for MACA."""

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import FormattedText


def color_print(*args):
    """
    Print arguments with optional color formatting.
    
    Args:
        *args: Mixed arguments - strings are printed as-is, tuples of (color, text) 
               are printed with the specified color.
    
    Example:
        color_print(('ansigreen', 'Hello '), 'world!')
        # Prints "Hello world!" where "Hello " is green and "world!" is default color
    """
    formatted_parts = []
    
    for arg in args:
        if isinstance(arg, tuple) and len(arg) == 2:
            # It's a (color, text) tuple
            color, text = arg
            formatted_parts.append((color, str(text)))
        else:
            # It's a regular argument, convert to string with default formatting
            formatted_parts.append(('', str(arg)))
    
    print_formatted_text(FormattedText(formatted_parts))
