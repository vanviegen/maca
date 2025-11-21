#!/usr/bin/env python3
"""Integration tests for MACA using debug LLM responses."""

import sys
import tempfile
import shutil
import json
import subprocess
from pathlib import Path
from typing import TypedDict, List, Dict, Union, Optional


class TestCase(TypedDict):
    """Definition of a single test case."""
    name: str
    task: str
    responses: List[Union[str, Dict]]  # str for text response, dict for respond call
    expected_files: Dict[str, str]  # path -> expected content
    expected_commit_msg: Optional[str]  # Expected HEAD commit message (without "MACA: " prefix)


# Test case definitions
TEST_CASES: List[TestCase] = [
    {
        'name': 'Simple File Creation',
        'task': 'Create a hello.txt file with "Hello, World!"',
        'responses': [
            """Creating a hello.txt file

~maca~ 1 OVERWRITE
path: hello.txt
content: ~maca~start~
Hello, World!

~maca~end~

~maca~ 2 OUTPUT
text: Created hello.txt with greeting

~maca~ 3 PROPOSE_MERGE
message: ~maca~start~
Add hello.txt

Created hello.txt with greeting message
~maca~end~

"""
        ],
        'expected_files': {
            'hello.txt': 'Hello, World!\n'
        },
        'expected_commit_msg': None  # Commit msg is "Session 1" in non-interactive mode
    },
    {
        'name': 'File Update',
        'task': 'Update README.md with a description',
        'responses': [
            """Updating README.md

~maca~ 1 UPDATE
path: README.md
search: ~maca~start~
# Test Project
~maca~end~
replace: ~maca~start~
# Test Project

This is a test.
~maca~end~

~maca~ 2 OUTPUT
text: Updated README.md with description

~maca~ 3 PROPOSE_MERGE
message: ~maca~start~
Update README.md

Added description to README
~maca~end~

"""
        ],
        'expected_files': {
            'README.md': '# Test Project\n\nThis is a test.\n'
        },
        'expected_commit_msg': None
    },
    {
        'name': 'Multi-Step Task',
        'task': 'Create a todo.txt file for the project',
        'responses': [
            """Reading README to understand project

~maca~ 1 READ
path: README.md

~maca~ 2 NOTES
text: README contains "# Test Project"

""",
            """Creating todo.txt based on project name

~maca~ 1 OVERWRITE
path: todo.txt
content: ~maca~start~
TODO for Test Project:
- Write tests
- Run tests

~maca~end~

~maca~ 2 OUTPUT
text: Created todo.txt

~maca~ 3 PROPOSE_MERGE
message: ~maca~start~
Add todo.txt

Created todo list for Test Project
~maca~end~

"""
        ],
        'expected_files': {
            'todo.txt': 'TODO for Test Project:\n- Write tests\n- Run tests\n'
        },
        'expected_commit_msg': None
    },
]


from maca import MACA
from utils import set_cprint_callback
from llm import set_debug_llm_responses
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

    # Initialize git and configure it (before any commits)
    subprocess.run(['git', 'init'], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@example.com'], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test User'], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(['git', 'config', 'commit.gpgsign', 'false'], cwd=repo_path, check=True, capture_output=True)

    # Create an initial file and commit
    (repo_path / 'README.md').write_text('# Test Project\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'Initial commit'], cwd=repo_path, check=True, capture_output=True)

    return repo_path


def teardown_test_repo(repo_path):
    """Remove the temporary test repository."""
    shutil.rmtree(repo_path)


def build_llm_responses(responses: List[Union[str, Dict]]) -> List[Dict]:
    """Convert simplified response format to full LLM response format."""
    llm_responses = []

    for i, response in enumerate(responses):
        # All responses are now text (no more tool calls)
        llm_responses.append({
            'message': {
                'role': 'assistant',
                'content': response if isinstance(response, str) else json.dumps(response)
            },
            'cost': 1000,
            'usage': {'prompt_tokens': 100 + i*50, 'completion_tokens': 50 + i*10}
        })

    return llm_responses


def get_commit_message(repo_path: Path) -> str:
    """Get the HEAD commit message."""
    result = subprocess.run(
        ['git', 'log', '-1', '--pretty=%B'],
        cwd=repo_path,
        capture_output=True,
        text=True
    )
    return result.stdout.strip()


def run_test_case(test_case: TestCase):
    """Execute a single test case."""
    print(f"\n=== Test: {test_case['name']} ===")

    # Setup
    repo_path = setup_test_repo()
    output = TestOutput()
    set_cprint_callback(output.callback)

    try:
        # Convert responses to LLM format
        llm_responses = build_llm_responses(test_case['responses'])
        set_debug_llm_responses(llm_responses)

        # Create MACA instance
        maca = MACA(
            directory=str(repo_path),
            task=test_case['task'],
            model='test-model',
            non_interactive=True,
            verbose=False
        )

        # Run MACA
        maca.run()

        # Verify files
        for file_path, expected_content in test_case['expected_files'].items():
            full_path = repo_path / file_path
            assert full_path.exists(), f"{file_path} should exist"
            actual_content = full_path.read_text()
            assert actual_content == expected_content, \
                f"{file_path} content mismatch:\nExpected: {expected_content!r}\nActual: {actual_content!r}"

        # Verify commit message if specified
        if test_case.get('expected_commit_msg'):
            commit_msg = get_commit_message(repo_path)
            expected_msg = f"MACA: {test_case['expected_commit_msg']}"
            # The commit message includes the preservation note, so just check if it starts correctly
            assert commit_msg.startswith(expected_msg), \
                f"Commit message mismatch:\nExpected to start with: {expected_msg!r}\nActual: {commit_msg!r}"

        print(f"✓ Test passed: {test_case['name']}")

    finally:
        # Cleanup
        set_debug_llm_responses(None)
        set_cprint_callback(None)
        teardown_test_repo(repo_path)



def run_all_tests():
    """Run all integration tests."""
    print("Starting MACA integration tests...")

    passed = 0
    failed = 0

    for test_case in TEST_CASES:
        try:
            run_test_case(test_case)
            passed += 1
        except Exception as e:
            print(f"✗ Test failed: {test_case['name']}")
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
