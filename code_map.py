#!/usr/bin/env python3
"""Generate a code map for software projects in multiple languages.

This module uses tree-sitter to parse source files and generate a hierarchical
code map showing classes, functions, methods, and their relationships.

Supported: 165+ languages via tree-sitter-language-pack
"""

import sys
from pathlib import Path
from typing import Dict, List, Set, Optional
from dataclasses import dataclass, field

try:
    from tree_sitter_language_pack import get_parser
    from tree_sitter import Node
except ImportError:
    print("Error: tree-sitter-language-pack not installed", file=sys.stderr)
    print("Install with: pip install tree-sitter-language-pack", file=sys.stderr)
    sys.exit(1)


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


@dataclass
class FileInfo:
    """Represents a file (code or non-code)."""
    path: str
    lines: Optional[int] = None  # For text files
    bytes: Optional[int] = None  # For binary files or when line count unavailable


class LanguageConfig:
    """Configuration for parsing a specific language."""

    def __init__(self, name: str, extensions: List[str], node_types: Dict[str, List[str]], self_params: List[str] = None):
        """
        Initialize language configuration.

        Args:
            name: Language name (as used by tree-sitter-language-pack)
            extensions: File extensions for this language
            node_types: Mapping of definition types to tree-sitter node types
            self_params: Parameters to exclude (like 'self', 'this')
        """
        self.name = name
        self.extensions = extensions
        self.node_types = node_types
        self.self_params = self_params or []


# Comprehensive language configurations
LANGUAGE_CONFIGS = {
    # Common languages
    'python': LanguageConfig(
        name='python',
        extensions=['.py', '.pyw'],
        node_types={'class': ['class_definition'], 'function': ['function_definition']},
        self_params=['self', 'cls']
    ),
    'javascript': LanguageConfig(
        name='javascript',
        extensions=['.js', '.jsx', '.mjs', '.cjs'],
        node_types={'class': ['class_declaration'], 'function': ['function_declaration', 'method_definition', 'arrow_function']},
        self_params=['this']
    ),
    'typescript': LanguageConfig(
        name='typescript',
        extensions=['.ts', '.tsx'],
        node_types={'class': ['class_declaration'], 'interface': ['interface_declaration'], 'function': ['function_declaration', 'method_definition', 'arrow_function']},
        self_params=['this']
    ),
    'go': LanguageConfig(
        name='go',
        extensions=['.go'],
        node_types={'struct': ['type_declaration'], 'function': ['function_declaration', 'method_declaration']},
    ),
    'rust': LanguageConfig(
        name='rust',
        extensions=['.rs'],
        node_types={'struct': ['struct_item'], 'enum': ['enum_item'], 'trait': ['trait_item'], 'function': ['function_item']},
    ),
    'java': LanguageConfig(
        name='java',
        extensions=['.java'],
        node_types={'class': ['class_declaration'], 'interface': ['interface_declaration'], 'function': ['method_declaration']},
        self_params=['this']
    ),
    'c': LanguageConfig(
        name='c',
        extensions=['.c', '.h'],
        node_types={'struct': ['struct_specifier'], 'function': ['function_definition']},
    ),
    'cpp': LanguageConfig(
        name='cpp',
        extensions=['.cpp', '.hpp', '.cc', '.hh', '.cxx', '.hxx', '.c++', '.h++'],
        node_types={'class': ['class_specifier'], 'struct': ['struct_specifier'], 'function': ['function_definition']},
        self_params=['this']
    ),
    'ruby': LanguageConfig(
        name='ruby',
        extensions=['.rb', '.rake'],
        node_types={'class': ['class'], 'module': ['module'], 'function': ['method']},
        self_params=['self']
    ),
    'php': LanguageConfig(
        name='php',
        extensions=['.php'],
        node_types={'class': ['class_declaration'], 'interface': ['interface_declaration'], 'function': ['function_definition', 'method_declaration']},
        self_params=['this', '$this']
    ),
    'csharp': LanguageConfig(
        name='c_sharp',
        extensions=['.cs'],
        node_types={'class': ['class_declaration'], 'interface': ['interface_declaration'], 'struct': ['struct_declaration'], 'function': ['method_declaration']},
        self_params=['this']
    ),
    'swift': LanguageConfig(
        name='swift',
        extensions=['.swift'],
        node_types={'class': ['class_declaration'], 'struct': ['struct_declaration'], 'protocol': ['protocol_declaration'], 'function': ['function_declaration']},
        self_params=['self']
    ),
    'kotlin': LanguageConfig(
        name='kotlin',
        extensions=['.kt', '.kts'],
        node_types={'class': ['class_declaration'], 'interface': ['interface_declaration'], 'function': ['function_declaration']},
        self_params=['this']
    ),
    'scala': LanguageConfig(
        name='scala',
        extensions=['.scala'],
        node_types={'class': ['class_definition'], 'trait': ['trait_definition'], 'function': ['function_definition']},
        self_params=['this']
    ),
    'dart': LanguageConfig(
        name='dart',
        extensions=['.dart'],
        node_types={'class': ['class_definition'], 'function': ['function_signature', 'method_signature']},
        self_params=['this']
    ),
    'elixir': LanguageConfig(
        name='elixir',
        extensions=['.ex', '.exs'],
        node_types={'module': ['defmodule'], 'function': ['def', 'defp']},
    ),
    'haskell': LanguageConfig(
        name='haskell',
        extensions=['.hs', '.lhs'],
        node_types={'function': ['function_declaration', 'signature']},
    ),
    'lua': LanguageConfig(
        name='lua',
        extensions=['.lua'],
        node_types={'function': ['function_declaration', 'function_definition']},
        self_params=['self']
    ),
    'perl': LanguageConfig(
        name='perl',
        extensions=['.pl', '.pm'],
        node_types={'function': ['subroutine_declaration_statement']},
    ),
    'r': LanguageConfig(
        name='r',
        extensions=['.r', '.R'],
        node_types={'function': ['function_definition']},
    ),
    'julia': LanguageConfig(
        name='julia',
        extensions=['.jl'],
        node_types={'function': ['function_definition']},
    ),
    'ocaml': LanguageConfig(
        name='ocaml',
        extensions=['.ml', '.mli'],
        node_types={'module': ['module_definition'], 'function': ['value_definition']},
    ),
    'zig': LanguageConfig(
        name='zig',
        extensions=['.zig'],
        node_types={'struct': ['struct_declaration'], 'function': ['function_declaration']},
    ),
    'bash': LanguageConfig(
        name='bash',
        extensions=['.sh', '.bash'],
        node_types={'function': ['function_definition']},
    ),
}


class CodeMapGenerator:
    """Generates code maps from source files in multiple languages."""

    def __init__(self, directory: Path):
        """Initialize the generator with a directory."""
        self.directory = directory
        self.definitions: Dict[str, Definition] = {}
        self.file_infos: List[FileInfo] = []
        self.id_counter = 1
        self.parsers: Dict[str, any] = {}

    def _is_binary_file(self, file_path: Path) -> bool:
        """
        Simple heuristic to detect if a file is binary.
        
        Checks for null bytes in the first 8KB.
        """
        try:
            with open(file_path, 'rb') as f:
                chunk = f.read(8192)
                return b'\x00' in chunk
        except Exception:
            return True  # If we can't read it, assume binary

    def _collect_all_files(self) -> None:
        """
        Collect all files in the directory, respecting .gitignore.
        
        Uses get_matching_files from tools module with .gitignore support.
        """
        # Import here to avoid circular dependency
        import tools
        
        # Get all files, respecting .gitignore
        all_files = tools.get_matching_files(
            worktree_path=self.directory,
            include="**",
            exclude=[".git/**", ".claude/**"],
            exclude_files=[".gitignore", ".macaignore"],
        )
        
        for file_path in all_files:
            rel_path = file_path.relative_to(self.directory)
            
            # Check if binary
            if self._is_binary_file(file_path):
                # Binary file - store size in bytes
                try:
                    size = file_path.stat().st_size
                    self.file_infos.append(FileInfo(
                        path=str(rel_path),
                        bytes=size
                    ))
                except Exception:
                    pass
            else:
                # Text file - try to count lines
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='strict') as f:
                        line_count = sum(1 for _ in f)
                    self.file_infos.append(FileInfo(
                        path=str(rel_path),
                        lines=line_count
                    ))
                except Exception:
                    # If we can't read as text, treat as binary
                    try:
                        size = file_path.stat().st_size
                        self.file_infos.append(FileInfo(
                            path=str(rel_path),
                            bytes=size
                        ))
                    except Exception:
                        pass
    
    def _load_language(self, lang_name: str) -> Optional[any]:
        """Load a tree-sitter language parser."""
        if lang_name in self.parsers:
            return self.parsers[lang_name]

        try:
            parser = get_parser(lang_name)
            self.parsers[lang_name] = parser
            return parser
        except Exception as e:
            print(f"Warning: Failed to load {lang_name}: {e}", file=sys.stderr)
            return None

    def _detect_language(self, file_path: str) -> Optional[LanguageConfig]:
        """Detect the language of a file based on its extension."""
        suffix = Path(file_path).suffix.lower()
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
            if child.type in ['parameters', 'parameter_list', 'formal_parameters', 'parameter_declarations']:
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

    def _parse_file(self, file_path: str, lang_config: LanguageConfig) -> None:
        """Parse a single source file and extract definitions."""
        parser = self._load_language(lang_config.name)
        if not parser:
            return

        try:
            source = (self.directory / file_path).read_bytes()
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
            if def_type in ['class', 'struct', 'interface', 'enum', 'trait', 'module', 'protocol']:
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

    def generate_map(self) -> str:
        """Generate a code map for all files in the directory.

        Returns:
            String representation of the code map
        """
        # Collect all files first
        self._collect_all_files()

        # Find all source files that can be parsed
        source_files = []
        for file_info in self.file_infos:
            # Only try to parse text files with known extensions
            if file_info.lines is not None:
                lang_config = self._detect_language(file_info.path)
                if lang_config:
                    source_files.append(file_info.path)

        # Parse source files for code structures
        for file_path in source_files:
            lang_config = self._detect_language(file_path)
            if lang_config:
                self._parse_file(file_path, lang_config)

        # Assign IDs and resolve references
        self._assign_ids_and_resolve_references()

        # Generate output
        return self._format_output()

    def _format_output(self) -> str:
        """Format the definitions and file info into a readable code map."""
        lines = []

        # Group definitions by file
        by_file: Dict[str, List[Definition]] = {}
        for definition in self.definitions.values():
            if definition.file_path not in by_file:
                by_file[definition.file_path] = []
            by_file[definition.file_path].append(definition)

        # Create file path -> FileInfo mapping
        file_info_map = {fi.path: fi for fi in self.file_infos}

        # Sort files
        for file_info in sorted(self.file_infos, key=lambda f: f.path):
            file_path_str = file_info.path

            # Show file with size info
            if file_info.lines is not None:
                lines.append(f"{file_path_str} [{file_info.lines} lines]")
            else:
                lines.append(f"{file_path_str} [{file_info.bytes} bytes]")

            # If this file has code definitions, show them
            if file_path_str in by_file:
                definitions = by_file[file_path_str]

                # Separate containers (classes, structs, etc.) and top-level functions
                containers = [d for d in definitions if d.type in ['class', 'struct', 'interface', 'enum', 'trait', 'module', 'protocol']]
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
    dir_path = Path(directory)
    if not dir_path.exists():
        raise ValueError(f"Directory does not exist: {directory}")
    
    generator = CodeMapGenerator(dir_path)
    return generator.generate_map()


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
