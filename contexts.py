#!/usr/bin/env python3
"""Context classes for managing different types of LLM interactions."""

import json
import os
import urllib.request
from pathlib import Path
from typing import Dict, List, Any, Optional

import tools


class ContextError(Exception):
    """Context operation failed."""
    pass


class BaseContext:
    """Base class for all contexts."""

    def __init__(
        self,
        context_id: str,
        context_type: str,
        model: str,
        max_response_chars: int = 10000,
        api_key: Optional[str] = None
    ):
        """
        Initialize a context.

        Args:
            context_id: Unique identifier for this context
            context_type: Type of context (main, code_analysis, research, etc.)
            model: Model to use for this context
            max_response_chars: Maximum characters in response
            api_key: OpenRouter API key (defaults to env var)
        """
        self.context_id = context_id
        self.context_type = context_type
        self.model = model
        self.max_response_chars = max_response_chars
        self.api_key = api_key or os.environ.get('OPENROUTER_API_KEY')
        self.messages = []
        self.last_usage = {}

        if not self.api_key:
            raise ContextError("OPENROUTER_API_KEY not set")

        # Load system prompt
        self._load_system_prompt()

    def _load_system_prompt(self):
        """Load the system prompt from a markdown file."""
        # Find the prompts directory (next to the script)
        script_dir = Path(__file__).parent
        prompts_dir = script_dir / 'prompts'

        prompt_file = f'{self.context_type}.md'
        prompt_path = prompts_dir / prompt_file

        if not prompt_path.exists():
            raise ContextError(f"System prompt not found: {prompt_path}")

        system_prompt = prompt_path.read_text()

        self.messages.append({
            'role': 'system',
            'content': system_prompt
        })

    def add_message(self, role: str, content: str):
        """Add a message to the context."""
        self.messages.append({
            'role': role,
            'content': content
        })

    def get_tool_schemas(self) -> List[Dict]:
        """Get tool schemas for this context type."""
        if self.context_type == 'main':
            return tools.get_tool_schemas('main')
        else:
            return tools.get_tool_schemas('subcontext')

    def call_llm(self) -> Dict[str, Any]:
        """
        Call the LLM and return the response.

        Returns:
            Dict with 'message' and 'tool_calls' keys
        """
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.api_key}',
            'HTTP-Referer': 'https://github.com/vanviegen/aai',
            'X-Title': 'AAI - Agentic Coding Assistant'
        }

        # Get tool schemas
        tool_schemas = self.get_tool_schemas()

        data = {
            'model': self.model,
            'messages': self.messages,
            'tools': tool_schemas,
            'tool_choice': 'required',  # Force tool use
        }

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
        self.last_usage = result.get('usage', {})

        # Add assistant message to history
        self.messages.append(message)

        # Extract tool calls
        tool_calls = message.get('tool_calls', [])

        # Validate: must have exactly one tool call
        if len(tool_calls) != 1:
            raise ContextError(f"Expected exactly 1 tool call, got {len(tool_calls)}")

        return {
            'message': message,
            'tool_call': tool_calls[0],
            'usage': self.last_usage
        }

    def execute_tool(self, tool_call: Dict) -> Any:
        """Execute a tool call and return the result."""
        tool_name = tool_call['function']['name']
        arguments = json.loads(tool_call['function']['arguments'])

        context_tool_type = 'main' if self.context_type == 'main' else 'subcontext'

        result = tools.execute_tool(tool_name, arguments, context_tool_type)
        return result

    def add_tool_result(self, tool_call: Dict, result: Any):
        """Add a tool result to the message history."""
        # Format tool result for the API
        self.messages.append({
            'role': 'tool',
            'tool_call_id': tool_call['id'],
            'content': str(result)[:self.max_response_chars]
        })


class MainContext(BaseContext):
    """Main orchestrator context."""

    def __init__(self, model: str = "anthropic/claude-sonnet-4.5", api_key: Optional[str] = None):
        super().__init__(
            context_id='main',
            context_type='main',
            model=model,
            max_response_chars=100000,  # Main context can have large responses
            api_key=api_key
        )


class CodeAnalysisContext(BaseContext):
    """Context for analyzing code and maintaining AI-ARCHITECTURE.md."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", max_response_chars: int = 2000, api_key: Optional[str] = None):
        super().__init__(
            context_id=unique_name,
            context_type='code_analysis',
            model=model,
            max_response_chars=max_response_chars,
            api_key=api_key
        )


class ResearchContext(BaseContext):
    """Context for web research and information gathering."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", max_response_chars: int = 2000, api_key: Optional[str] = None):
        super().__init__(
            context_id=unique_name,
            context_type='research',
            model=model,
            max_response_chars=max_response_chars,
            api_key=api_key
        )


class ImplementationContext(BaseContext):
    """Context for implementing code based on specifications."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", max_response_chars: int = 2000, api_key: Optional[str] = None):
        super().__init__(
            context_id=unique_name,
            context_type='implementation',
            model=model,
            max_response_chars=max_response_chars,
            api_key=api_key
        )


class ReviewContext(BaseContext):
    """Context for reviewing code quality and correctness."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", max_response_chars: int = 2000, api_key: Optional[str] = None):
        super().__init__(
            context_id=unique_name,
            context_type='review',
            model=model,
            max_response_chars=max_response_chars,
            api_key=api_key
        )


class MergeContext(BaseContext):
    """Context for resolving merge conflicts."""

    def __init__(self, unique_name: str, model: str = "anthropic/claude-sonnet-4.5", max_response_chars: int = 2000, api_key: Optional[str] = None):
        super().__init__(
            context_id=unique_name,
            context_type='merge',
            model=model,
            max_response_chars=max_response_chars,
            api_key=api_key
        )


# Context type registry for creation
CONTEXT_TYPES = {
    'code_analysis': CodeAnalysisContext,
    'research': ResearchContext,
    'implementation': ImplementationContext,
    'review': ReviewContext,
    'merge': MergeContext
}


def create_context(unique_name: str, context_type: str, model: str = "auto", max_response_chars: int = 2000) -> BaseContext:
    """
    Create a new context of the specified type.

    Args:
        unique_name: Unique identifier for the context
        context_type: Type of context to create
        model: Model to use (or "auto" for default)
        max_response_chars: Maximum response characters

    Returns:
        New context instance
    """
    if context_type not in CONTEXT_TYPES:
        raise ContextError(f"Unknown context type: {context_type}")

    if model == "auto":
        # Use default model for each context type
        model = "anthropic/claude-sonnet-4.5"

    context_class = CONTEXT_TYPES[context_type]
    return context_class(unique_name, model, max_response_chars)
