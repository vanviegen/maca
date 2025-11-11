#!/usr/bin/env python3
"""Generate a code map for software projects in multiple languages.

This module uses tree-sitter to parse source files and generate a hierarchical
code map showing classes, functions, methods, and their relationships.

Supported languages: Python, JavaScript, TypeScript, Go, Rust, Java, C, C++, Ruby
"""

import sys
from pathlib import Path
from typing import Dict, List, Set, Optional, Tuple
from dataclasses import dataclass, field
from tree_sitter import Parser, Language, Node

# Language imports - will be loaded dynamically
LANGUAGE_MODULES = {
    'python': 'tree_sitter_python',
    'javascript': 'tree_sitter_javascript',
    'typescript': 'tree_sitter_typescript',
    'go': 'tree_sitter_go',
    'rust': 'tree_sitter_rust',
    'java': 'tree_sitter_java',
    'c': 'tree_sitter_c',
    'cpp': 'tree_sitter_cpp',
    'ruby': 'tree_sitter_ruby',
}


@dataclass
class Definition:
    """Represents a code definition (class, function, method, struct, interface, etc.)."""
    name: str
    type: str  # 'class', 'function', 'method', 'struct', 'interface', etc.
    start_line: int
    end_line: int
    file_path: str
    params: List[str]
    parent: Optional[str] = None  # For methods, the class/struct name
    id: Optional[str] = None
    uses: Set[str] = field(default_factory=set)


class LanguageConfig:
    """Configuration for parsing a specific language."""

    def __init__(self, name: str, extensions: List[str], node_types: Dict[str, List[str]]):
        """
        Initialize language configuration.

        Args:
            name: Language name
            extensions: File extensions for this language
            node_types: Mapping of definition types to tree-sitter node types
        """
        self.name = name
        self.extensions = extensions
        self.node_types = node_types
        self.self_params = []  # Parameters to exclude (like 'self', 'this')


# Language configurations
LANGUAGE_CONFIGS = {
    'python': LanguageConfig(
        name='python',
        extensions=['.py'],
        node_types={
            'class': ['class_definition'],
            'function': ['function_definition'],
        }
    ),
    'javascript': LanguageConfig(
        name='javascript',
        extensions=['.js', '.jsx', '.mjs'],
        node_types={
            'class': ['class_declaration'],
            'function': ['function_declaration', 'method_definition', 'arrow_function'],
        }
    ),
    'typescript': LanguageConfig(
        name='typescript',
        extensions=['.ts', '.tsx'],
        node_types={
            'class': ['class_declaration'],
            'interface': ['interface_declaration'],
            'function': ['function_declaration', 'method_definition', 'arrow_function'],
        }
    ),
    'go': LanguageConfig(
        name='go',
        extensions=['.go'],
        node_types={
            'struct': ['type_declaration'],  # Go uses type declarations
            'function': ['function_declaration', 'method_declaration'],
        }
    ),
    'rust': LanguageConfig(
        name='rust',
        extensions=['.rs'],
        node_types={
            'struct': ['struct_item'],
            'enum': ['enum_item'],
            'trait': ['trait_item'],
            'function': ['function_item'],
        }
    ),
    'java': LanguageConfig(
        name='java',
        extensions=['.java'],
        node_types={
            'class': ['class_declaration'],
            'interface': ['interface_declaration'],
            'function': ['method_declaration'],
        }
    ),
    'c': LanguageConfig(
        name='c',
        extensions=['.c', '.h'],
        node_types={
            'struct': ['struct_specifier'],
            'function': ['function_definition'],
        }
    ),
    'cpp': LanguageConfig(
        name='cpp',
        extensions=['.cpp', '.hpp', '.cc', '.hh', '.cxx', '.hxx'],
        node_types={
            'class': ['class_specifier'],
            'struct': ['struct_specifier'],
            'function': ['function_definition'],
        }
    ),
    'ruby': LanguageConfig(
        name='ruby',
        extensions=['.rb'],
        node_types={
            'class': ['class'],
            'module': ['module'],
            'function': ['method'],
        }
    ),
}

# Set self parameters for languages
LANGUAGE_CONFIGS['python'].self_params = ['self', 'cls']
LANGUAGE_CONFIGS['javascript'].self_params = ['this']
LANGUAGE_CONFIGS['typescript'].self_params = ['this']
LANGUAGE_CONFIGS['ruby'].self_params = ['self']


class CodeMapGenerator:
    """Generates code maps from source files in multiple languages."""

    def __init__(self):
        """Initialize the generator."""
        self.definitions: Dict[str, Definition] = {}
        self.id_counter = 1
        self.parsers: Dict[str, Parser] = {}
        self.loaded_languages: Set[str] = set()

    def _load_language(self, lang_name: str) -> Optional[Parser]:
        """Load a tree-sitter language parser."""
        if lang_name in self.parsers:
            return self.parsers[lang_name]

        if lang_name not in LANGUAGE_MODULES:
            return None

        try:
            module_name = LANGUAGE_MODULES[lang_name]
            module = __import__(module_name)

            # Special handling for TypeScript which has two languages
            if lang_name == 'typescript':
                language = Language(module.language_typescript())
            else:
                language = Language(module.language())

            parser = Parser(language)
            self.parsers[lang_name] = parser
            self.loaded_languages.add(lang_name)
            return parser
        except ImportError:
            print(f"Warning: {module_name} not installed, skipping {lang_name} files", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Warning: Failed to load {lang_name}: {e}", file=sys.stderr)
            return None

    def _detect_language(self, file_path: Path) -> Optional[LanguageConfig]:
        """Detect the language of a file based on its extension."""
        suffix = file_path.suffix.lower()
        for lang_name, config in LANGUAGE_CONFIGS.items():
            if suffix in config.extensions:
                return config
        return None

    def _get_definition_key(self, file_path: str, name: str, parent: Optional[str] = None) -> str:
        """Generate a unique key for a definition."""
        if parent:
            return f"{file_path}::{parent}.{name}"
        return f"{file_path}::{name}"

    def _extract_identifier(self, node: Node) -> Optional[str]:
        """Extract identifier name from a node."""
        if node.type == 'identifier':
            return node.text.decode('utf-8')

        # Look for identifier in children
        for child in node.children:
            if child.type == 'identifier':
                return child.text.decode('utf-8')
            # Handle type_identifier (TypeScript, Go, etc.)
            if child.type == 'type_identifier':
                return child.text.decode('utf-8')
            # Handle name field
            if child.type == 'name':
                return child.text.decode('utf-8')

        return None

    def _extract_params(self, node: Node, lang_config: LanguageConfig) -> List[str]:
        """Extract parameter names from a function/method definition."""
        params = []

        # Find the parameters node
        params_node = None
        for child in node.children:
            if child.type in ['parameters', 'parameter_list', 'formal_parameters']:
                params_node = child
                break

        if not params_node:
            return params

        # Extract parameter identifiers
        for child in params_node.children:
            param_name = None

            if child.type == 'identifier':
                param_name = child.text.decode('utf-8')
            elif child.type in ['parameter', 'parameter_declaration', 'formal_parameter']:
                # Look for identifier in parameter
                param_name = self._extract_identifier(child)
            elif 'parameter' in child.type or 'param' in child.type:
                param_name = self._extract_identifier(child)

            if param_name and param_name not in lang_config.self_params:
                params.append(param_name)

        return params

    def _extract_identifiers(self, node: Node) -> Set[str]:
        """Extract all identifier names used in a node's body."""
        identifiers = set()

        def visit(n: Node):
            if n.type in ['identifier', 'type_identifier']:
                identifiers.add(n.text.decode('utf-8'))
            for child in n.children:
                visit(child)

        visit(node)
        return identifiers

    def _parse_file(self, file_path: Path, lang_config: LanguageConfig) -> None:
        """Parse a single source file and extract definitions."""
        parser = self._load_language(lang_config.name)
        if not parser:
            return

        try:
            source = file_path.read_bytes()
            tree = parser.parse(source)
            root = tree.root_node

            self._extract_definitions(root, source, str(file_path), lang_config)
        except Exception as e:
            print(f"Warning: Failed to parse {file_path}: {e}", file=sys.stderr)

    def _extract_definitions(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        lang_config: LanguageConfig,
        parent_class: Optional[str] = None
    ) -> None:
        """Recursively extract definitions from AST."""

        # Check if this is a container type (class, struct, interface, etc.)
        is_container = False
        container_type = None
        for def_type, node_types in lang_config.node_types.items():
            if def_type in ['class', 'struct', 'interface', 'enum', 'trait', 'module']:
                if node.type in node_types:
                    is_container = True
                    container_type = def_type
                    break

        if is_container:
            name = self._extract_identifier(node)
            body_node = None

            # Find the body/block
            for child in node.children:
                if child.type in ['block', 'class_body', 'declaration_list', 'field_declaration_list', 'body']:
                    body_node = child
                    break

            if name:
                definition = Definition(
                    name=name,
                    type=container_type,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    file_path=file_path,
                    params=[],
                    parent=None
                )
                key = self._get_definition_key(file_path, name)
                self.definitions[key] = definition

                # Process body for methods
                if body_node:
                    self._extract_definitions(body_node, source, file_path, lang_config, parent_class=name)

            return

        # Check if this is a function/method
        is_function = False
        for def_type, node_types in lang_config.node_types.items():
            if def_type == 'function':
                if node.type in node_types:
                    is_function = True
                    break

        if is_function:
            func_name = self._extract_identifier(node)
            body_node = None

            # Find the body/block
            for child in node.children:
                if child.type in ['block', 'body', 'statement_block', 'compound_statement']:
                    body_node = child
                    break

            if func_name:
                params = self._extract_params(node, lang_config)

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
                    identifiers = self._extract_identifiers(body_node)
                    definition.uses = identifiers

            return

        # Recursively process children for other node types
        for child in node.children:
            self._extract_definitions(child, source, file_path, lang_config, parent_class)

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
        """Generate a code map for all supported files in the directory.

        Args:
            directory: Path to the directory to scan

        Returns:
            String representation of the code map
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            raise ValueError(f"Directory does not exist: {directory}")

        # Find all source files
        source_files = []
        for config in LANGUAGE_CONFIGS.values():
            for ext in config.extensions:
                source_files.extend(dir_path.rglob(f"*{ext}"))

        source_files = sorted(set(source_files))

        # Parse all files
        for file_path in source_files:
            # Skip hidden directories and common exclusions
            if any(part.startswith('.') for part in file_path.parts):
                continue
            if any(part in ['node_modules', '__pycache__', 'target', 'build', 'dist'] for part in file_path.parts):
                continue

            lang_config = self._detect_language(file_path)
            if lang_config:
                self._parse_file(file_path, lang_config)

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

            # Separate containers (classes, structs, etc.) and top-level functions
            containers = [d for d in definitions if d.type in ['class', 'struct', 'interface', 'enum', 'trait', 'module']]
            functions = [d for d in definitions if d.type == 'function']

            # Sort by line number
            containers.sort(key=lambda d: d.start_line)
            functions.sort(key=lambda d: d.start_line)

            # Output containers and their methods
            for container in containers:
                uses_str = f", uses {' '.join(sorted(container.uses))}" if container.uses else ""
                lines.append(f"  {container.id} {container.type} {container.name} [lines {container.start_line}-{container.end_line}{uses_str}]")

                # Find methods for this container
                methods = [d for d in definitions if d.type == 'method' and d.parent == container.name]
                methods.sort(key=lambda d: d.start_line)

                for method in methods:
                    params_str = ", ".join(method.params) if method.params else ""
                    uses_str = f", uses {' '.join(sorted(method.uses))}" if method.uses else ""
                    lines.append(f"    {method.id} method {method.name}({params_str}) [lines {method.start_line}-{method.end_line}{uses_str}]")

            # Output top-level functions
            for func in functions:
                params_str = ", ".join(func.params) if func.params else ""
                uses_str = f", uses {' '.join(sorted(func.uses))}" if func.uses else ""
                lines.append(f"  {func.id} function {func.name}({params_str}) [lines {func.start_line}-{func.end_line}{uses_str}]")

        return "\n".join(lines)


def generate_code_map(directory: str) -> str:
    """Generate a code map for a software project.

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
