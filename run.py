from maca import MACA, ContextError
from utils import cprint, C_BAD
import argparse
import os
import sys

parser = argparse.ArgumentParser(
    prog='maca',
    description='Multi-Agent Coding Assistant',
)
parser.add_argument('task', nargs='*', help='Initial task description')
parser.add_argument('-m', '--model', default='anthropic/claude-sonnet-4.5',
                    help='Model to use for main context')
parser.add_argument('-d', '--directory', default='.',
                    help='Project directory (default: current directory)')
parser.add_argument('-n', '--non-interactive', action='store_true',
                    help='Run in non-interactive mode (requires task argument)')
parser.add_argument('-v', '--verbose', action='store_true',
                    help='Enable verbose logging mode')
args = parser.parse_args()

# Validate non-interactive mode
task_str = ' '.join(args.task) if args.task else None
if args.non_interactive and not task_str:
    cprint(C_BAD, 'Error: --non-interactive (-n) requires a task argument')
    sys.exit(1)

# Resolve API key at startup
api_key = os.environ.get('OPENROUTER_API_KEY')
if not api_key:
    print("Error: OPENROUTER_API_KEY environment variable not set", file=sys.stderr)
    sys.exit(1)

# Create MACA instance and run
maca = MACA(args.directory, task_str, args.model, api_key, args.non_interactive, args.verbose)
maca.run()
