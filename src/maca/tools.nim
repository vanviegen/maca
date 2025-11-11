## Tool system with schema management and execution

import std/[json, tables, os, strutils, sequtils, re, random]
import git_ops, docker_ops

type
  ToolInfo* = object
    schema*: JsonNode
    handler*: proc(args: JsonNode): JsonNode

  ReadyResult* = object
    isReady*: bool
    result*: JsonNode

var
  toolRegistry*: Table[string, ToolInfo] = initTable[string, ToolInfo]()
  macaInstance*: ref RootObj = nil  # Will be set by main module

# Forward declarations for tool implementations
proc readFiles(args: JsonNode): JsonNode
proc listFiles(args: JsonNode): JsonNode
proc updateFiles(args: JsonNode): JsonNode
proc search(args: JsonNode): JsonNode
proc shell(args: JsonNode): JsonNode
proc subcontextComplete(args: JsonNode): JsonNode
proc askMainQuestion(args: JsonNode): JsonNode
proc getUserInput(args: JsonNode): JsonNode
proc createSubcontext(args: JsonNode): JsonNode
proc runOneshotPerFile(args: JsonNode): JsonNode
proc continueSubcontext(args: JsonNode): JsonNode
proc mainComplete(args: JsonNode): JsonNode
proc updateFilesAndComplete(args: JsonNode): JsonNode

proc registerTool*(name: string, schema: JsonNode, handler: proc(args: JsonNode): JsonNode) =
  ## Register a tool in the registry
  toolRegistry[name] = ToolInfo(schema: schema, handler: handler)

proc getToolSchemas*(toolNames: seq[string], addRationale = true): seq[JsonNode] =
  ## Get tool schemas for the specified tool names
  result = @[]
  for name in toolNames:
    if not toolRegistry.hasKey(name):
      raise newException(ValueError, "Unknown tool: " & name)

    var schema = toolRegistry[name].schema.copy()

    # Add rationale parameter if requested
    if addRationale:
      schema["function"]["parameters"]["properties"]["rationale"] = %*{
        "type": "string",
        "description": "**Very brief** (max 20 words) explanation of why this tool is being called and what you expect to accomplish"
      }
      schema["function"]["parameters"]["required"].add(%"rationale")

    result.add(schema)

proc executeTool*(name: string, args: JsonNode): JsonNode =
  ## Execute a tool with the given arguments
  if not toolRegistry.hasKey(name):
    raise newException(ValueError, "Unknown tool: " & name)

  # Remove rationale from arguments before calling (it's just for logging)
  var execArgs = args.copy()
  if execArgs.hasKey("rationale"):
    execArgs.delete("rationale")

  return toolRegistry[name].handler(execArgs)

# Helper functions
proc checkPath*(path: string, worktreePath: string): string =
  ## Validate that a path is within the worktree and doesn't escape via symlinks
  let resolved = expandFilename(path)
  let worktreeAbs = expandFilename(worktreePath)

  if not resolved.startsWith(worktreeAbs):
    raise newException(ValueError, "Path '" & path & "' is outside the worktree directory")

  return resolved

proc getMatchingFiles*(worktreePath: string, include: seq[string], exclude: seq[string]): seq[string] =
  ## Get list of files matching include/exclude glob patterns
  result = @[]

  # Simple glob matching - walks directory and applies patterns
  for file in walkDirRec(worktreePath):
    let relPath = file.relativePath(worktreePath)

    # Check include patterns
    var included = false
    for pattern in include:
      if relPath.match(re(pattern.replace("**", ".*").replace("*", "[^/]*"))):
        included = true
        break

    if not included:
      continue

    # Check exclude patterns
    var excluded = false
    for pattern in exclude:
      if relPath.match(re(pattern.replace("**", ".*").replace("*", "[^/]*"))):
        excluded = true
        break

    if not excluded and fileExists(file):
      result.add(relPath)

# Tool implementations
proc readFiles(args: JsonNode): JsonNode =
  ## Read one or more files, optionally with line range limits
  let filePaths = args["file_paths"].getElems().mapIt(it.getStr())
  let startLine = if args.hasKey("start_line"): args["start_line"].getInt() else: 1
  let maxLines = if args.hasKey("max_lines"): args["max_lines"].getInt() else: 250

  result = newJArray()
  # TODO: Get worktreePath from maca instance
  # For now, placeholder implementation

proc listFiles(args: JsonNode): JsonNode =
  ## List files matching include/exclude patterns
  # TODO: Implement
  result = %*{"total_count": 0, "files": []}

proc updateFiles(args: JsonNode): JsonNode =
  ## Update one or more files with new content
  # TODO: Implement
  result = %*{"status": "ok"}

proc search(args: JsonNode): JsonNode =
  ## Search for a regex pattern in file contents
  # TODO: Implement
  result = newJArray()

proc shell(args: JsonNode): JsonNode =
  ## Execute a shell command in a Docker container
  # TODO: Implement with docker_ops
  result = %*{"stdout": "", "stderr": "", "exit_code": 0}

proc subcontextComplete(args: JsonNode): JsonNode =
  ## Signal that subcontext is complete
  let resultStr = args["result"].getStr()
  result = %*{
    "ready": true,
    "message": "Task completed with result:\n" & resultStr
  }

proc askMainQuestion(args: JsonNode): JsonNode =
  ## Ask the main context a question
  let question = args["question"].getStr()
  result = %*{
    "ready": true,
    "message": "The subcontext has a question for the main context:\n" & question
  }

proc getUserInput(args: JsonNode): JsonNode =
  ## Get input from the user interactively
  # TODO: Implement with nimline
  result = %"user input"

proc createSubcontext(args: JsonNode): JsonNode =
  ## Create a new subcontext
  # TODO: Implement with context module
  result = %*{"status": "created"}

proc runOneshotPerFile(args: JsonNode): JsonNode =
  ## Run one-shot file processor on matching files
  # TODO: Implement
  result = %*{}

proc continueSubcontext(args: JsonNode): JsonNode =
  ## Continue running an existing subcontext
  # TODO: Implement
  result = %*{"status": "continued"}

proc mainComplete(args: JsonNode): JsonNode =
  ## Signal that the entire task is complete
  let resultStr = args["result"].getStr()
  result = %*{
    "ready": true,
    "message": resultStr
  }

proc updateFilesAndComplete(args: JsonNode): JsonNode =
  ## Update files and signal completion (file_processor only)
  # TODO: Implement
  result = %*{
    "ready": true,
    "message": "Files updated"
  }

# Register all tools with their schemas
proc initTools*() =
  ## Initialize and register all tools

  registerTool("read_files", %*{
    "type": "function",
    "function": {
      "name": "read_files",
      "description": "Read one or more files, optionally with line range limits. IMPORTANT: Read ALL relevant files in a SINGLE call for efficiency.",
      "parameters": {
        "type": "object",
        "properties": {
          "file_paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of file paths to read"
          },
          "start_line": {
            "type": "integer",
            "description": "Line number to start reading from (1-indexed)",
            "default": 1
          },
          "max_lines": {
            "type": "integer",
            "description": "Maximum number of lines to read per file",
            "default": 250
          }
        },
        "required": ["file_paths"],
        "additionalProperties": false
      }
    }
  }, readFiles)

  registerTool("list_files", %*{
    "type": "function",
    "function": {
      "name": "list_files",
      "description": "List files in the worktree matching include/exclude glob patterns. Returns random sampling if more files match than max_files.",
      "parameters": {
        "type": "object",
        "properties": {
          "include": {
            "type": ["string", "array"],
            "description": "Glob pattern(s) to include (default: ** for all files)"
          },
          "exclude": {
            "type": ["string", "array"],
            "description": "Glob pattern(s) to exclude (default: .* for hidden files)"
          },
          "max_files": {
            "type": "integer",
            "description": "Maximum number of files to return",
            "default": 50
          }
        },
        "additionalProperties": false
      }
    }
  }, listFiles)

  registerTool("update_files", %*{
    "type": "function",
    "function": {
      "name": "update_files",
      "description": "Update one or more files with new content. Supports full file write or search-and-replace.",
      "parameters": {
        "type": "object",
        "properties": {
          "updates": {
            "type": "array",
            "items": {"type": "object"},
            "description": "List of update specifications"
          }
        },
        "required": ["updates"],
        "additionalProperties": false
      }
    }
  }, updateFiles)

  registerTool("subcontext_complete", %*{
    "type": "function",
    "function": {
      "name": "subcontext_complete",
      "description": "Signal that your subtask is complete and return the result to the main context.",
      "parameters": {
        "type": "object",
        "properties": {
          "result": {
            "type": "string",
            "description": "Summary of what was accomplished"
          }
        },
        "required": ["result"],
        "additionalProperties": false
      }
    }
  }, subcontextComplete)

  registerTool("main_complete", %*{
    "type": "function",
    "function": {
      "name": "main_complete",
      "description": "Signal that the ENTIRE user task is complete and ready to end the session.",
      "parameters": {
        "type": "object",
        "properties": {
          "result": {
            "type": "string",
            "description": "Answer or summary of what was accomplished"
          },
          "commit_msg": {
            "type": ["string", "null"],
            "description": "Optional git commit message summarizing changes"
          }
        },
        "required": ["result", "commit_msg"],
        "additionalProperties": false
      }
    }
  }, mainComplete)

  # TODO: Register remaining tools (search, shell, create_subcontext, etc.)
