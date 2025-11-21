"""LLM API interaction and streaming."""

from typing import List, Dict, Any, Optional
import json
import urllib.request
import time
import re
import os
import sys

from logger import log
from utils import cprint, C_INFO, C_BAD


# Global cumulative cost tracking
_cumulative_cost = 0

# Debug/testing support
_debug_llm_responses = None
_debug_llm_index = 0


# Get API key from environment (will be checked when actually needed)
api_key = os.environ.get('OPENROUTER_API_KEY')


class LLMStreamReader:
    """Reads and processes streaming responses from the LLM API."""
    
    def __init__(self):
        self.buffer = ""
        self.message = None
        self.usage = None
        self._partial_arg_json = ""
    
    def process_chunk(self, chunk_str: str):
        """Process a chunk of streaming data."""
        self.buffer += chunk_str
        
        while '\n' in self.buffer:
            line_end = self.buffer.find('\n')
            line = self.buffer[:line_end].strip()
            self.buffer = self.buffer[line_end + 1:]
            
            if not line or line.startswith(':'):
                continue
            
            if line.startswith('data: '):
                data_str = line[6:]
                if data_str == '[DONE]':
                    break
                
                try:
                    data_obj = json.loads(data_str)
                    delta = data_obj.get('choices', [{}])[0].get('delta', {})

                    # Handle text content
                    if 'content' in delta and delta['content'] is not None:
                        if self.message is None:
                            self.message = {'role': 'assistant', 'content': ''}
                        if 'content' not in self.message:
                            self.message['content'] = ''
                        self.message['content'] += delta['content']

                    # Handle tool calls
                    if 'tool_calls' in delta:
                        if self.message is None:
                            self.message = {'role': 'assistant', 'tool_calls': []}
                        if 'tool_calls' not in self.message:
                            self.message['tool_calls'] = []

                        for tool_call_delta in delta['tool_calls']:
                            idx = tool_call_delta.get('index', 0)

                            while len(self.message['tool_calls']) <= idx:
                                self.message['tool_calls'].append({
                                    'id': '', 'type': 'function',
                                    'function': {'name': '', 'arguments': ''}
                                })

                            tc = self.message['tool_calls'][idx]
                            if 'id' in tool_call_delta:
                                tc['id'] = tool_call_delta['id']
                            if 'type' in tool_call_delta:
                                tc['type'] = tool_call_delta['type']
                            if 'function' in tool_call_delta:
                                if 'name' in tool_call_delta['function']:
                                    tc['function']['name'] = tool_call_delta['function']['name']
                                if 'arguments' in tool_call_delta['function']:
                                    tc['function']['arguments'] += tool_call_delta['function']['arguments']
                                    self._partial_arg_json = tc['function']['arguments']

                    if 'usage' in data_obj:
                        self.usage = data_obj['usage']
                
                except json.JSONDecodeError:
                    pass
    
    def _find_truncation_point(self, json_str: str) -> List[str]:
        """Find the path to the current field being written in truncated JSON."""
        path, i = [], 0
        
        while i < len(json_str) and json_str[i].isspace():
            i += 1
        
        if i >= len(json_str):
            return path
        
        stack = []
        
        while i < len(json_str):
            while i < len(json_str) and json_str[i].isspace():
                i += 1
            if i >= len(json_str):
                break
                
            c = json_str[i]
            
            if c == '"':
                start = i
                i += 1
                while i < len(json_str) and json_str[i] != '"':
                    i += 2 if json_str[i] == '\\' else 1
                if i >= len(json_str):
                    break
                i += 1
                
                # Check if this is a key
                j = i
                while j < len(json_str) and json_str[j].isspace():
                    j += 1
                if j < len(json_str) and json_str[j] == ':':
                    key = json_str[start+1:i-1].replace('\\"', '"').replace('\\\\', '\\')
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
                m = re.match(r'-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?', json_str[i:])
                i += len(m.group(0)) if m else 1
                if stack and stack[-1][0] == 'obj':
                    stack.pop()
            elif json_str[i:i+4] in ('true', 'null') or json_str[i:i+5] == 'false':
                i += 4 if json_str[i] == 't' or json_str[i] == 'n' else 5
                if stack and stack[-1][0] == 'obj':
                    stack.pop()
            else:
                i += 1
        
        return [x[1] for x in stack]
    
    def get_status(self) -> str:
        """Get a human-readable status of what's currently being streamed."""
        if not self._partial_arg_json:
            return "receiving"
        
        try:
            path = self._find_truncation_point(self._partial_arg_json)
            if path:
                return "receiving " + path[0].replace('_', ' ')
        except:
            pass
        
        return "receiving"
    
    def get_bytes_received(self) -> int:
        """Get the number of bytes received so far."""
        return len(self._partial_arg_json)


def call_llm(
    model: str,
    messages: List[Dict[str, Any]],
    tool_schemas: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Call the OpenRouter LLM API with retry logic and streaming.

    Args:
        model: Model identifier (e.g., "anthropic/claude-sonnet-4.5")
        messages: List of message dicts with role and content
        tool_schemas: Optional list of tool schemas (for backwards compatibility)

    Returns:
        Dict with:
        - message: Assistant message dict
        - cost: Cost in microdollars (integer)
        - usage: Usage dict with token counts

    Raises:
        Exception: If API call fails after 3 retries
    """
    # Check if we're in debug mode
    global _debug_llm_responses, _debug_llm_index
    if _debug_llm_responses is not None:
        if _debug_llm_index >= len(_debug_llm_responses):
            raise Exception(f"Debug LLM responses exhausted (needed {_debug_llm_index + 1}, have {len(_debug_llm_responses)})")

        response = _debug_llm_responses[_debug_llm_index]
        _debug_llm_index += 1

        # Log the call
        log(tag='llm_call', model=model, cost=response.get('cost', 0),
            prompt_tokens=response.get('usage', {}).get('prompt_tokens', 0),
            completion_tokens=response.get('usage', {}).get('completion_tokens', 0),
            duration=0)

        return response

    # Check API key when actually making a real API call
    if not api_key:
        cprint(C_BAD, 'Error: OPENROUTER_API_KEY environment variable not set')
        sys.exit(1)

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'HTTP-Referer': 'https://github.com/vanviegen/maca',
        'X-Title': 'MACA - Multi-Agent Coding Assistant'
    }

    data = {
        'model': model,
        'messages': messages,
        'usage': {"include": True},
        'stream': True,
        'streamOptions': {'includeUsage': True}
        # 'reasoning': {
        #     'effort': 'medium'
        # }
    }

    # Add tool-related fields only if tool_schemas is provided
    if tool_schemas:
        data['tools'] = tool_schemas
        data['tool_choice'] = 'required'

    # Retry up to 3 times
    last_error = None
    for retry in range(3):
        start_time = time.time()

        try:
            cprint(C_INFO, "LLM: starting...", end="")

            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=json.dumps(data).encode('utf-8'),
                headers=headers
            )

            # Stream the response
            stream = LLMStreamReader()

            with urllib.request.urlopen(req) as response:
                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break

                    stream.process_chunk(chunk.decode('utf-8'))

                    # Show progress with current field being written
                    print('\r\033[K', end='')
                    cprint(C_INFO, f'LLM: {stream.get_status()}... ({stream.get_bytes_received()} bytes)', end='')

            # Clear progress line
            print('\r\033[K', end='')
            cprint(C_INFO, f'LLM: done! ({stream.get_bytes_received()} bytes)')

            # Validate we got a message
            if stream.message is None:
                raise Exception("No message received from stream")

            # Clean up empty content: OpenAI/OpenRouter format allows null content with tool calls
            # Anthropic requires non-empty content, but OpenRouter should handle the conversion
            if stream.message.get('content') == '':
                stream.message['content'] = None

            # Calculate cost
            cost = int(stream.usage.get('cost', 0) * 1_000_000) if stream.usage else 0  # Convert dollars to microdollars
            duration = time.time() - start_time

            # Update global cumulative cost
            global _cumulative_cost
            _cumulative_cost += cost

            # Log the call
            log(tag='llm_call', model=model, cost=cost, 
                prompt_tokens=stream.usage.get('prompt_tokens', 0), 
                completion_tokens=stream.usage.get('completion_tokens', 0), 
                duration=duration)

            return {
                'message': stream.message,
                'cost': cost,
                'usage': stream.usage or {}
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


def set_debug_llm_responses(responses: Any):
    """
    Set debug LLM responses for testing.

    Args:
        responses: List of response dicts, each containing 'message', 'cost', and 'usage'.
                   Set to None to disable debug mode and use real API calls.

    Each response dict should have:
        - message: Assistant message dict with 'role', 'content', and optional 'tool_calls'
        - cost: Cost in microdollars (integer)
        - usage: Usage dict with 'prompt_tokens' and 'completion_tokens'
    """
    global _debug_llm_responses, _debug_llm_index, _cumulative_cost
    _debug_llm_responses = responses
    _debug_llm_index = 0
    _cumulative_cost = 0
