#!/usr/bin/env python3
"""Integration tests for MACA using debug LLM responses."""

import os
import sys
import tempfile
import shutil
import json
from pathlib import Path

# Add parent directory to path to import maca modules
sys.path.insert(0, str(Path(__file__).parent))

from maca import MACA
from utils import set_debug_llm_responses, set_cprint_callback
import git_ops


class TestOutput:
    """Captures output from cprint for test assertions."""
    def __init__(self):
        self.lines = []

    def callback(self, text, end):
        self.lines.append(text + end)

    def get_output(self):
        return ''.join(self.lines)

    def clear(self):
        self.lines = []


def setup_test_repo():
    """Create a temporary git repository for testing."""
    test_dir = tempfile.mkdtemp(prefix='maca_test_')
    repo_path = Path(test_dir)

    # Configure git for the test repo
    os.system(f'cd {repo_path} && git config user.email "test@example.com"')
    os.system(f'cd {repo_path} && git config user.name "Test User"')

    # Initialize git repo
    git_ops.init_git_repo(repo_path)

    # Create an initial file
    (repo_path / 'README.md').write_text('# Test Project\n')
    os.system(f'cd {repo_path} && git add README.md && git commit -m "Initial commit"')

    return repo_path


def teardown_test_repo(repo_path):
    """Remove the temporary test repository."""
    shutil.rmtree(repo_path)


def test_simple_file_creation():
    """Test creating a simple file."""
    print("\n=== Test: Simple File Creation ===")

    # Setup
    repo_path = setup_test_repo()
    output = TestOutput()
    set_cprint_callback(output.callback)

    try:
        # Create debug LLM response that creates a file
        tool_call = {
            'id': 'call_1',
            'type': 'function',
            'function': {
                'name': 'respond',
                'arguments': json.dumps({
                    'thoughts': 'Creating a hello.txt file',
                    'file_updates': [{
                        'path': 'hello.txt',
                        'overwrite': 'Hello, World!\n',
                        'summary': 'Create hello.txt with greeting'
                    }],
                    'file_change_description': 'Add hello.txt',
                    'user_output': 'Created hello.txt with greeting',
                    'done': True
                })
            }
        }

        responses = [
            {
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [tool_call]
                },
                'cost': 1000,
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}
            }
        ]

        set_debug_llm_responses(responses)

        # Create MACA instance
        maca = MACA(
            directory=str(repo_path),
            task='Create a hello.txt file with "Hello, World!"',
            model='test-model',
            api_key='test-key',
            non_interactive=True,
            verbose=False
        )

        # Run MACA
        maca.run()

        # Verify file was created in the main branch
        hello_file = repo_path / 'hello.txt'
        assert hello_file.exists(), "hello.txt should exist"
        content = hello_file.read_text()
        assert content == 'Hello, World!\n', f"Expected 'Hello, World!\\n', got {content!r}"

        print("✓ Test passed: File created successfully")

    finally:
        # Cleanup
        set_debug_llm_responses(None)
        set_cprint_callback(None)
        teardown_test_repo(repo_path)


def test_file_update():
    """Test updating an existing file."""
    print("\n=== Test: File Update ===")

    # Setup
    repo_path = setup_test_repo()
    output = TestOutput()
    set_cprint_callback(output.callback)

    try:
        # Create debug LLM response that updates README.md
        tool_call = {
            'id': 'call_2',
            'type': 'function',
            'function': {
                'name': 'respond',
                'arguments': json.dumps({
                    'thoughts': 'Updating README.md',
                    'file_updates': [{
                        'path': 'README.md',
                        'update': [{
                            'search': '# Test Project\n',
                            'replace': '# Test Project\n\nThis is a test.\n'
                        }],
                        'summary': 'Add description to README'
                    }],
                    'file_change_description': 'Update README.md',
                    'user_output': 'Updated README.md with description',
                    'done': True
                })
            }
        }

        responses = [
            {
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [tool_call]
                },
                'cost': 1000,
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}
            }
        ]

        set_debug_llm_responses(responses)

        # Create MACA instance
        maca = MACA(
            directory=str(repo_path),
            task='Update README.md with a description',
            model='test-model',
            api_key='test-key',
            non_interactive=True,
            verbose=False
        )

        # Run MACA
        maca.run()

        # Verify file was updated
        readme_file = repo_path / 'README.md'
        content = readme_file.read_text()
        assert 'This is a test.' in content, f"Expected updated content, got {content!r}"

        print("✓ Test passed: File updated successfully")

    finally:
        # Cleanup
        set_debug_llm_responses(None)
        set_cprint_callback(None)
        teardown_test_repo(repo_path)


def test_multi_step_task():
    """Test a task that requires multiple LLM calls."""
    print("\n=== Test: Multi-Step Task ===")

    # Setup
    repo_path = setup_test_repo()
    output = TestOutput()
    set_cprint_callback(output.callback)

    try:
        # First call: read README to understand what to do
        tool_call_1 = {
            'id': 'call_3',
            'type': 'function',
            'function': {
                'name': 'respond',
                'arguments': json.dumps({
                    'thoughts': 'Reading README to understand project',
                    'file_reads': [{'path': 'README.md'}],
                    'notes_for_context': 'README contains "# Test Project"'
                })
            }
        }

        # Second call: create the file based on what we read
        tool_call_2 = {
            'id': 'call_4',
            'type': 'function',
            'function': {
                'name': 'respond',
                'arguments': json.dumps({
                    'thoughts': 'Creating todo.txt based on project name',
                    'file_updates': [{
                        'path': 'todo.txt',
                        'overwrite': 'TODO for Test Project:\n- Write tests\n- Run tests\n',
                        'summary': 'Create todo.txt'
                    }],
                    'file_change_description': 'Add todo.txt',
                    'user_output': 'Created todo.txt',
                    'done': True
                })
            }
        }

        responses = [
            {
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [tool_call_1]
                },
                'cost': 1000,
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}
            },
            {
                'message': {
                    'role': 'assistant',
                    'content': '',
                    'tool_calls': [tool_call_2]
                },
                'cost': 1000,
                'usage': {'prompt_tokens': 150, 'completion_tokens': 60}
            }
        ]

        set_debug_llm_responses(responses)

        # Create MACA instance
        maca = MACA(
            directory=str(repo_path),
            task='Create a todo.txt file for the project',
            model='test-model',
            api_key='test-key',
            non_interactive=True,
            verbose=False
        )

        # Run MACA
        maca.run()

        # Verify file was created
        todo_file = repo_path / 'todo.txt'
        assert todo_file.exists(), "todo.txt should exist"
        content = todo_file.read_text()
        assert 'Test Project' in content, f"Expected project name in todo, got {content!r}"

        print("✓ Test passed: Multi-step task completed successfully")

    finally:
        # Cleanup
        set_debug_llm_responses(None)
        set_cprint_callback(None)
        teardown_test_repo(repo_path)


def run_all_tests():
    """Run all integration tests."""
    print("Starting MACA integration tests...")

    tests = [
        test_simple_file_creation,
        test_file_update,
        test_multi_step_task,
    ]

    passed = 0
    failed = 0

    for test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"✗ Test failed: {test_func.__name__}")
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*60}")
    print(f"Tests completed: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    return failed == 0


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
