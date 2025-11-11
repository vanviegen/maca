## Tool system with schema management and execution

import std/[json, tables, os, strutils, sequtils, re, random, strformat, algorithm]
import docker_ops

type
  ToolInfo = object
    schema: JsonNode
    handler: proc(args: JsonNode, ctx: ToolContext): JsonNode

  ToolContext* = ref object
    ## Context passed to tool handlers containing session state
    worktreePath*: string
    repoRoot*: string
    sessionId*: int

  ReadyResult* = object
    ## Signals that a context is ready to complete
    isReady*: bool
    result*: JsonNode

var
  toolRegistry: Table[string, ToolInfo] = initTable[string, ToolInfo]()

proc registerTool(name: string, schema: JsonNode, handler: proc(args: JsonNode, ctx: ToolContext): JsonNode) =
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

proc executeTool*(name: string, args: JsonNode, ctx: ToolContext): JsonNode =
  ## Execute a tool with the given arguments
  if not toolRegistry.hasKey(name):
    raise newException(ValueError, "Unknown tool: " & name)

  # Remove rationale from arguments before calling (it's just for logging)
  var execArgs = args.copy()
  if execArgs.hasKey("rationale"):
    execArgs.delete("rationale")

  return toolRegistry[name].handler(execArgs, ctx)

# Helper functions
proc checkPath(path: string, worktreePath: string): string =
  ## Validate that a path is within the worktree and doesn't escape via symlinks
  let absPath = if path.isAbsolute(): path else: worktreePath / path
  let resolved = expandFilename(absPath)
  let worktreeAbs = expandFilename(worktreePath)

  if not resolved.startsWith(worktreeAbs):
    raise newException(ValueError, "Path '" & path & "' is outside the worktree directory")

  return resolved

proc globToRegex(pattern: string): Regex =
  ## Convert a glob pattern to a regex
  var regexStr = pattern
  regexStr = regexStr.replace(".", "\\.")
  regexStr = regexStr.replace("**", "\x00")  # Temp placeholder
  regexStr = regexStr.replace("*", "[^/]*")
  regexStr = regexStr.replace("\x00", ".*")
  return re(regexStr)

proc matchesGlob(path: string, pattern: string): bool =
  ## Check if a path matches a glob pattern
  try:
    let regex = globToRegex(pattern)
    return path.match(regex)
  except:
    return false

proc getMatchingFiles(worktreePath: string, includePatterns: seq[string], excludePatterns: seq[string]): seq[string] =
  ## Get list of files matching include/exclude glob patterns
  result = @[]

  # Walk directory and collect matching files
  for file in walkDirRec(worktreePath):
    # Convert to relative path
    let relPath = file.relativePath(worktreePath)

    # Skip .maca and .scratch directories
    if relPath.startsWith(".maca") or relPath.startsWith(".scratch"):
      continue

    # Check include patterns
    var included = false
    for pattern in includePatterns:
      if matchesGlob(relPath, pattern):
        included = true
        break

    if not included:
      continue

    # Check exclude patterns
    var excluded = false
    for pattern in excludePatterns:
      if matchesGlob(relPath, pattern):
        excluded = true
        break

    if not excluded:
      if fileExists(file):
        result.add(relPath)

# Tool implementations
proc readFiles(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Read one or more files, optionally with line range limits
  let filePaths = args["file_paths"].getElems().mapIt(it.getStr())
  let startLine = if args.hasKey("start_line"): args["start_line"].getInt() else: 1
  let maxLines = if args.hasKey("max_lines"): args["max_lines"].getInt() else: 250

  result = newJArray()

  for filePath in filePaths:
    try:
      let fullPath = checkPath(filePath, ctx.worktreePath)

      if not fileExists(fullPath):
        result.add(%*{
          "file_path": filePath,
          "error": "File not found",
          "data": "",
          "remaining_lines": 0
        })
        continue

      let lines = readFile(fullPath).splitLines()
      let totalLines = lines.len
      let endLine = min(startLine - 1 + maxLines, totalLines)
      let selectedLines = lines[max(0, startLine - 1) ..< endLine]
      let data = selectedLines.join("\n")
      let remaining = max(0, totalLines - endLine)

      result.add(%*{
        "file_path": filePath,
        "data": data,
        "remaining_lines": remaining,
        "total_lines": totalLines
      })

    except Exception as e:
      result.add(%*{
        "file_path": filePath,
        "error": e.msg,
        "data": "",
        "remaining_lines": 0
      })

proc listFiles(args: JsonNode, ctx: ToolContext): JsonNode =
  ## List files matching include/exclude patterns
  var includePatterns: seq[string]
  if args.hasKey("include"):
    if args["include"].kind == JString:
      includePatterns = @[args["include"].getStr()]
    elif args["include"].kind == JArray:
      includePatterns = args["include"].getElems().mapIt(it.getStr())
    else:
      includePatterns = @["**"]
  else:
    includePatterns = @["**"]

  var excludePatterns: seq[string]
  if args.hasKey("exclude"):
    if args["exclude"].kind == JString:
      excludePatterns = @[args["exclude"].getStr()]
    elif args["exclude"].kind == JArray:
      excludePatterns = args["exclude"].getElems().mapIt(it.getStr())
    else:
      excludePatterns = @[".*"]
  else:
    excludePatterns = @[".*"]

  let maxFiles = if args.hasKey("max_files"): args["max_files"].getInt() else: 50

  var matchingFiles = getMatchingFiles(ctx.worktreePath, includePatterns, excludePatterns)
  let totalCount = matchingFiles.len

  # Random sample if too many files
  if matchingFiles.len > maxFiles:
    randomize()
    shuffle(matchingFiles)
    matchingFiles = matchingFiles[0 ..< maxFiles]

  # Build file info
  var filesInfo = newJArray()
  for relPath in matchingFiles:
    let fullPath = ctx.worktreePath / relPath
    var info = %*{"path": relPath}

    try:
      let stat = getFileInfo(fullPath)
      info["bytes"] = %stat.size

      # Check if executable
      when defined(posix):
        if (stat.permissions * {fpUserExec, fpGroupExec, fpOthersExec}) != {}:
          info["type"] = %"executable"

      # Try to count lines for text files (skip large files)
      if stat.size < 1024 * 1024:  # Only for files < 1MB
        try:
          let lines = readFile(fullPath).splitLines()
          info["lines"] = %lines.len
        except:
          discard  # Binary or invalid encoding

    except:
      discard  # Just return path if we can't stat

    filesInfo.add(info)

  # Sort by path
  var sortedFiles = filesInfo.getElems()
  sortedFiles.sort(proc(a, b: JsonNode): int =
    cmp(a["path"].getStr(), b["path"].getStr())
  )

  result = %*{
    "total_count": totalCount,
    "files": sortedFiles
  }

proc updateFiles(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Update one or more files with new content
  let updates = args["updates"].getElems()

  for update in updates:
    let filePath = update["file_path"].getStr()
    let fullPath = checkPath(filePath, ctx.worktreePath)

    # Ensure parent directory exists
    createDir(fullPath.parentDir())

    if update.hasKey("data"):
      # Full file write
      writeFile(fullPath, update["data"].getStr())

    elif update.hasKey("old_data") and update.hasKey("new_data"):
      # Search and replace
      if not fileExists(fullPath):
        raise newException(ValueError, "Cannot search/replace in non-existent file: " & filePath)

      var content = readFile(fullPath)
      let oldData = update["old_data"].getStr()
      let newData = update["new_data"].getStr()
      let allowMultiple = if update.hasKey("allow_multiple"): update["allow_multiple"].getBool() else: false

      let count = content.count(oldData)
      if count == 0:
        raise newException(ValueError, &"Search string not found in {filePath}")
      elif count > 1 and not allowMultiple:
        raise newException(ValueError, &"Search string appears {count} times in {filePath}, but allow_multiple=false")

      if allowMultiple:
        content = content.replace(oldData, newData)
      else:
        # Replace first occurrence
        let idx = content.find(oldData)
        content = content[0 ..< idx] & newData & content[idx + oldData.len .. ^1]

      writeFile(fullPath, content)

    else:
      raise newException(ValueError, "Invalid update specification: " & $update)

  result = %*{"status": "ok"}

proc search(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Search for a regex pattern in file contents
  let pattern = args["regex"].getStr()
  let regex = re(pattern)

  var includePatterns: seq[string]
  if args.hasKey("include"):
    if args["include"].kind == JString:
      includePatterns = @[args["include"].getStr()]
    elif args["include"].kind == JArray:
      includePatterns = args["include"].getElems().mapIt(it.getStr())
    else:
      includePatterns = @["**"]
  else:
    includePatterns = @["**"]

  var excludePatterns: seq[string]
  if args.hasKey("exclude"):
    if args["exclude"].kind == JString:
      excludePatterns = @[args["exclude"].getStr()]
    elif args["exclude"].kind == JArray:
      excludePatterns = args["exclude"].getElems().mapIt(it.getStr())
    else:
      excludePatterns = @[".*"]
  else:
    excludePatterns = @[".*"]

  let maxResults = if args.hasKey("max_results"): args["max_results"].getInt() else: 10
  let linesBefore = if args.hasKey("lines_before"): args["lines_before"].getInt() else: 2
  let linesAfter = if args.hasKey("lines_after"): args["lines_after"].getInt() else: 2

  result = newJArray()
  var resultCount = 0

  let matchingFiles = getMatchingFiles(ctx.worktreePath, includePatterns, excludePatterns)

  for relPath in matchingFiles:
    if resultCount >= maxResults:
      break

    let fullPath = ctx.worktreePath / relPath

    try:
      let lines = readFile(fullPath).splitLines()

      for i, line in lines:
        if resultCount >= maxResults:
          break

        if line.match(regex):
          let startIdx = max(0, i - linesBefore)
          let endIdx = min(lines.len - 1, i + linesAfter)
          let contextLines = lines[startIdx .. endIdx]

          result.add(%*{
            "file_path": relPath,
            "line_number": i + 1,
            "lines": contextLines.join("\n")
          })

          inc resultCount

    except:
      # Skip files that can't be read
      continue

proc shell(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Execute a shell command in a Docker container
  let command = args["command"].getStr()
  let dockerImage = if args.hasKey("docker_image"): args["docker_image"].getStr() else: "debian:stable"

  var dockerRuns: seq[string] = @[]
  if args.hasKey("docker_runs"):
    dockerRuns = args["docker_runs"].getElems().mapIt(it.getStr())

  let head = if args.hasKey("head"): args["head"].getInt() else: 50
  let tail = if args.hasKey("tail"): args["tail"].getInt() else: 50

  let containerResult = runInContainer(
    command = command,
    worktreePath = ctx.worktreePath,
    repoRoot = ctx.repoRoot,
    dockerImage = dockerImage,
    dockerRuns = dockerRuns,
    head = head,
    tail = tail
  )

  result = %*{
    "stdout": containerResult.stdout,
    "stderr": containerResult.stderr,
    "exit_code": containerResult.exitCode
  }

proc subcontextComplete(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Signal that subcontext is complete
  let resultStr = args["result"].getStr()
  result = %*{
    "ready": true,
    "message": "Task completed with result:\n" & resultStr
  }

proc askMainQuestion(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Ask the main context a question
  let question = args["question"].getStr()
  result = %*{
    "ready": true,
    "message": "The subcontext has a question for the main context:\n" & question & "\n\nIf you want the subcontext to proceed, answer its question as guidance in a continue_subcontext call. If needed, you can get_user_input first."
  }

proc getUserInput(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Get input from the user interactively
  let prompt = args["prompt"].getStr()

  # Simple implementation without nimline
  stdout.write(prompt & "\n> ")
  stdout.flushFile()
  let input = readLine(stdin)

  result = %input

proc mainComplete(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Signal that the entire task is complete
  let resultStr = args["result"].getStr()
  let commitMsg = if args["commit_msg"].kind == JNull: "" else: args["commit_msg"].getStr()

  result = %*{
    "ready": true,
    "message": resultStr,
    "commit_msg": commitMsg
  }

proc updateFilesAndComplete(args: JsonNode, ctx: ToolContext): JsonNode =
  ## Update files and signal completion (file_processor only)
  let updates = args["updates"].getElems()

  # Perform updates (same as updateFiles)
  for update in updates:
    let filePath = update["file_path"].getStr()
    let fullPath = checkPath(filePath, ctx.worktreePath)

    createDir(fullPath.parentDir())

    if update.hasKey("data"):
      writeFile(fullPath, update["data"].getStr())
    elif update.hasKey("old_data") and update.hasKey("new_data"):
      if not fileExists(fullPath):
        raise newException(ValueError, "Cannot search/replace in non-existent file: " & filePath)

      var content = readFile(fullPath)
      let oldData = update["old_data"].getStr()
      let newData = update["new_data"].getStr()
      let allowMultiple = if update.hasKey("allow_multiple"): update["allow_multiple"].getBool() else: false

      let count = content.count(oldData)
      if count == 0:
        raise newException(ValueError, &"Search string not found in {filePath}")
      elif count > 1 and not allowMultiple:
        raise newException(ValueError, &"Search string appears {count} times in {filePath}, but allow_multiple=false")

      if allowMultiple:
        content = content.replace(oldData, newData)
      else:
        let idx = content.find(oldData)
        content = content[0 ..< idx] & newData & content[idx + oldData.len .. ^1]

      writeFile(fullPath, content)

  let resultStr = args["result"].getStr()
  result = %*{
    "ready": true,
    "message": resultStr
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
            "oneOf": [
              {"type": "string"},
              {"type": "array", "items": {"type": "string"}}
            ],
            "description": "Glob pattern(s) to include (default: ** for all files)"
          },
          "exclude": {
            "oneOf": [
              {"type": "string"},
              {"type": "array", "items": {"type": "string"}}
            ],
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

  registerTool("search", %*{
    "type": "function",
    "function": {
      "name": "search",
      "description": "Search for a regex pattern in file contents, filtering files by glob patterns.",
      "parameters": {
        "type": "object",
        "properties": {
          "regex": {
            "type": "string",
            "description": "Regular expression to search for"
          },
          "include": {
            "oneOf": [
              {"type": "string"},
              {"type": "array", "items": {"type": "string"}}
            ],
            "description": "Glob pattern(s) to include"
          },
          "exclude": {
            "oneOf": [
              {"type": "string"},
              {"type": "array", "items": {"type": "string"}}
            ],
            "description": "Glob pattern(s) to exclude"
          },
          "max_results": {
            "type": "integer",
            "description": "Maximum number of matches to return",
            "default": 10
          },
          "lines_before": {
            "type": "integer",
            "description": "Number of context lines before each match",
            "default": 2
          },
          "lines_after": {
            "type": "integer",
            "description": "Number of context lines after each match",
            "default": 2
          }
        },
        "required": ["regex"],
        "additionalProperties": false
      }
    }
  }, search)

  registerTool("shell", %*{
    "type": "function",
    "function": {
      "name": "shell",
      "description": "Execute a shell command in a Docker container.",
      "parameters": {
        "type": "object",
        "properties": {
          "command": {
            "type": "string",
            "description": "Shell command to execute"
          },
          "docker_image": {
            "type": "string",
            "description": "Base Docker image to use",
            "default": "debian:stable"
          },
          "docker_runs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of RUN commands for building the image"
          },
          "head": {
            "type": "integer",
            "description": "Number of lines to keep from start of output",
            "default": 50
          },
          "tail": {
            "type": "integer",
            "description": "Number of lines to keep from end of output",
            "default": 50
          }
        },
        "required": ["command"],
        "additionalProperties": false
      }
    }
  }, shell)

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

  registerTool("ask_main_question", %*{
    "type": "function",
    "function": {
      "name": "ask_main_question",
      "description": "Ask the main context a question when you need clarification.",
      "parameters": {
        "type": "object",
        "properties": {
          "question": {
            "type": "string",
            "description": "Question to ask the main context"
          }
        },
        "required": ["question"],
        "additionalProperties": false
      }
    }
  }, askMainQuestion)

  registerTool("get_user_input", %*{
    "type": "function",
    "function": {
      "name": "get_user_input",
      "description": "Get input from the user interactively.",
      "parameters": {
        "type": "object",
        "properties": {
          "prompt": {
            "type": "string",
            "description": "Prompt to display to the user"
          }
        },
        "required": ["prompt"],
        "additionalProperties": false
      }
    }
  }, getUserInput)

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
            "oneOf": [
              {"type": "string"},
              {"type": "null"}
            ],
            "description": "Optional git commit message summarizing changes"
          }
        },
        "required": ["result", "commit_msg"],
        "additionalProperties": false
      }
    }
  }, mainComplete)

  registerTool("update_files_and_complete", %*{
    "type": "function",
    "function": {
      "name": "update_files_and_complete",
      "description": "Update files and signal completion. Only for file_processor contexts.",
      "parameters": {
        "type": "object",
        "properties": {
          "updates": {
            "type": "array",
            "items": {"type": "object"},
            "description": "List of file update specifications"
          },
          "result": {
            "type": "string",
            "description": "Brief summary of what was done"
          }
        },
        "required": ["updates", "result"],
        "additionalProperties": false
      }
    }
  }, updateFilesAndComplete)
