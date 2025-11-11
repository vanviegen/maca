from maca import maca
import argparse

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

maca.run(args.directory, ' '.join(args.task) if args.task else None, args.model)
