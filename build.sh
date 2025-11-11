#!/bin/bash
# Build script for MACA (Nim version)

set -e

echo "Building MACA..."

# Check if Nim is installed
if ! command -v nim &> /dev/null; then
    echo "Error: Nim compiler not found!"
    echo "Please install Nim from https://nim-lang.org/install.html"
    echo "Or use choosenim: curl https://nim-lang.org/choosenim/init.sh -sSf | sh"
    exit 1
fi

# Build with release optimizations
nim c -d:release --opt:speed -o:maca src/maca.nim

echo "Build complete! Binary created: ./maca"
echo ""
echo "Usage:"
echo "  export OPENROUTER_API_KEY='your-key-here'"
echo "  ./maca                    # Run interactively"
echo "  ./maca 'your task here'   # Run with task"
echo "  ./maca --help             # Show help"
