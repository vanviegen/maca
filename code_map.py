#!/usr/bin/env python3
"""Generate a code map for a Python project.

This module uses tree-sitter to parse Python files and generate a hierarchical
code map showing classes, functions, methods, and their relationships.
"""

import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass
from tree_sitter import Parser, Language, Node
import tree_sitter_python


@dataclass
class Definition:
    """Represents a code definition (class, function, or method)."""
    name: str
    type: str  # 'class', 'function', or 'method'
    start_line: int
    end_line: int
    file_path: str
    params: List[str]
    parent: Optional[str] = None  # For methods, the class name
    id: Optional[str] = None
    uses: Set[str] = None  # IDs of definitions this one uses

    def __post_init__(self):
        if self.uses is None:
            self.uses = set()


class CodeMapGenerator:
    """Generates code maps from Python source files."""

    def __init__(self):
        """Initialize the parser with Python grammar."""
        self.parser = Parser(Language(tree_sitter_python.language()))
        self.definitions: Dict[str, Definition] = {}  # key: (file_path, name, parent?)
        self.id_counter = 1

    def _get_definition_key(self, file_path: str, name: str, parent: Optional[str] = None) -> str:
        """Generate a unique key for a definition."""
        if parent:
            return f"{file_path}::{parent}.{name}"
        return f"{file_path}::{name}"

    def _extract_params(self, parameters_node: Node) -> List[str]:
        """Extract parameter names and types from a function/method definition."""
        params = []
        if not parameters_node:
            return params

        for child in parameters_node.children:
            if child.type == 'identifier':
                params.append(child.text.decode('utf-8'))
            elif child.type == 'typed_parameter':
                # Get the identifier part before the type annotation
                for subchild in child.children:
                    if subchild.type == 'identifier':
                        params.append(subchild.text.decode('utf-8'))
                        break
            elif child.type == 'default_parameter':
                # Get the identifier part before the default value
                for subchild in child.children:
                    if subchild.type == 'identifier':
                        params.append(subchild.text.decode('utf-8'))
                        break
            elif child.type == 'typed_default_parameter':
                # Get the identifier part
                for subchild in child.children:
                    if subchild.type == 'identifier':
                        params.append(subchild.text.decode('utf-8'))
                        break

        # Remove 'self' and 'cls' from parameters
        params = [p for p in params if p not in ('self', 'cls')]
        return params

    def _extract_identifiers(self, node: Node, source: bytes) -> Set[str]:
        """Extract all identifier names used in a node's body."""
        identifiers = set()

        def visit(n: Node):
            if n.type == 'identifier':
                identifiers.add(n.text.decode('utf-8'))
            for child in n.children:
                visit(child)

        visit(node)
        return identifiers

    def _parse_file(self, file_path: Path) -> None:
        """Parse a single Python file and extract definitions."""
        try:
            source = file_path.read_bytes()
            tree = self.parser.parse(source)
            root = tree.root_node

            self._extract_definitions(root, source, str(file_path))
        except Exception as e:
            print(f"Warning: Failed to parse {file_path}: {e}", file=sys.stderr)

    def _extract_definitions(self, node: Node, source: bytes, file_path: str, parent_class: Optional[str] = None) -> None:
        """Recursively extract class and function definitions from AST."""
        if node.type == 'class_definition':
            # Extract class name
            class_name = None
            body_node = None

            for child in node.children:
                if child.type == 'identifier':
                    class_name = child.text.decode('utf-8')
                elif child.type == 'block':
                    body_node = child

            if class_name:
                # Create definition for the class
                definition = Definition(
                    name=class_name,
                    type='class',
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    file_path=file_path,
                    params=[],
                    parent=None
                )
                key = self._get_definition_key(file_path, class_name)
                self.definitions[key] = definition

                # Recursively process class body for methods
                if body_node:
                    self._extract_definitions(body_node, source, file_path, parent_class=class_name)

            # Don't recurse further - we've already processed the class body
            return

        elif node.type == 'function_definition':
            # Extract function/method name and parameters
            func_name = None
            params_node = None
            body_node = None

            for child in node.children:
                if child.type == 'identifier':
                    func_name = child.text.decode('utf-8')
                elif child.type == 'parameters':
                    params_node = child
                elif child.type == 'block':
                    body_node = child

            if func_name:
                params = self._extract_params(params_node)

                # Create definition for the function/method
                definition = Definition(
                    name=func_name,
                    type='method' if parent_class else 'function',
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    file_path=file_path,
                    params=params,
                    parent=parent_class
                )
                key = self._get_definition_key(file_path, func_name, parent_class)
                self.definitions[key] = definition

                # Extract identifiers used in the function body
                if body_node:
                    identifiers = self._extract_identifiers(body_node, source)
                    # Store for later reference resolution
                    definition.uses = identifiers

            # Don't recurse further - we don't want nested functions
            return

        # Recursively process children for other node types
        for child in node.children:
            self._extract_definitions(child, source, file_path, parent_class)

    def _assign_ids_and_resolve_references(self) -> None:
        """Assign IDs to definitions and resolve cross-references."""
        # First pass: assign IDs
        id_map = {}  # name -> id
        for key, definition in self.definitions.items():
            definition.id = f"#{self.id_counter}"
            id_map[definition.name] = definition.id
            self.id_counter += 1

        # Second pass: resolve references
        for key, definition in self.definitions.items():
            if definition.uses:
                resolved_uses = set()
                for identifier in definition.uses:
                    if identifier in id_map and identifier != definition.name:
                        resolved_uses.add(id_map[identifier])
                definition.uses = resolved_uses

    def generate_map(self, directory: str) -> str:
        """Generate a code map for all Python files in the directory.

        Args:
            directory: Path to the directory to scan

        Returns:
            String representation of the code map
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            raise ValueError(f"Directory does not exist: {directory}")

        # Find all Python files
        python_files = sorted(dir_path.rglob("*.py"))

        # Parse all files
        for file_path in python_files:
            # Skip hidden directories and common exclusions
            if any(part.startswith('.') for part in file_path.parts):
                continue
            if '__pycache__' in file_path.parts:
                continue

            self._parse_file(file_path)

        # Assign IDs and resolve references
        self._assign_ids_and_resolve_references()

        # Generate output
        return self._format_output(dir_path)

    def _format_output(self, base_path: Path) -> str:
        """Format the definitions into a readable code map."""
        lines = []

        # Group definitions by file
        by_file: Dict[str, List[Definition]] = {}
        for definition in self.definitions.values():
            if definition.file_path not in by_file:
                by_file[definition.file_path] = []
            by_file[definition.file_path].append(definition)

        # Sort files
        for file_path in sorted(by_file.keys()):
            # Get relative path
            try:
                rel_path = Path(file_path).relative_to(base_path)
            except ValueError:
                rel_path = Path(file_path)

            lines.append(str(rel_path))

            definitions = by_file[file_path]

            # Separate classes and top-level functions
            classes = [d for d in definitions if d.type == 'class']
            functions = [d for d in definitions if d.type == 'function']

            # Sort by line number
            classes.sort(key=lambda d: d.start_line)
            functions.sort(key=lambda d: d.start_line)

            # Output classes and their methods
            for cls in classes:
                uses_str = f", uses {', '.join(sorted(cls.uses))}" if cls.uses else ""
                lines.append(f"  class {cls.name} [{cls.id}, lines {cls.start_line}-{cls.end_line}{uses_str}]")

                # Find methods for this class
                methods = [d for d in definitions if d.type == 'method' and d.parent == cls.name]
                methods.sort(key=lambda d: d.start_line)

                for method in methods:
                    params_str = ", ".join(method.params) if method.params else ""
                    uses_str = f", uses {', '.join(sorted(method.uses))}" if method.uses else ""
                    lines.append(f"    method {method.name}({params_str}) [{method.id}, lines {method.start_line}-{method.end_line}{uses_str}]")

            # Output top-level functions
            for func in functions:
                params_str = ", ".join(func.params) if func.params else ""
                uses_str = f", uses {', '.join(sorted(func.uses))}" if func.uses else ""
                lines.append(f"  function {func.name}({params_str}) [{func.id}, lines {func.start_line}-{func.end_line}{uses_str}]")

        return "\n".join(lines)


def generate_code_map(directory: str) -> str:
    """Generate a code map for a Python project.

    Args:
        directory: Path to the directory to scan

    Returns:
        String representation of the code map
    """
    generator = CodeMapGenerator()
    return generator.generate_map(directory)


if __name__ == '__main__':
    # Get directory from command line or use current directory
    if len(sys.argv) > 1:
        directory = sys.argv[1]
    else:
        directory = '.'

    try:
        code_map = generate_code_map(directory)
        print(code_map)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
