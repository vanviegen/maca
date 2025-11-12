from maca import MACA, ContextError
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
args = parser.parse_args()

# Resolve API key at startup
api_key = os.environ.get('OPENROUTER_API_KEY')
if not api_key:
    print("Error: OPENROUTER_API_KEY environment variable not set", file=sys.stderr)
    sys.exit(1)

# Create MACA instance and run
maca = MACA(args.directory, ' '.join(args.task) if args.task else None, args.model, api_key)
maca.run()
