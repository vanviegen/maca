## Logs session events to per-context log files in human-readable format

import std/[json, times, strutils, random, os]

type
  Logger* = ref object
    file*: File
    seq*: int

var globalSeq = 0

proc findHeredocDelimiter(value: string): string =
  ## Find a delimiter that doesn't appear in the value
  if not value.contains("\nEOD"):
    return "EOD"

  # Generate a random string - chances of collision are infinitesimal
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
  result = ""
  for i in 0..9:
    result.add(chars[rand(chars.len - 1)])

proc newLogger*(repoRoot: string, sessionId: int, contextId: string): Logger =
  ## Initialize a session logger
  ##
  ## Args:
  ##   repoRoot: Path to the repository root
  ##   sessionId: The session ID number
  ##   contextId: The context identifier

  let sessionDir = repoRoot / ".maca" / $sessionId

  # Ensure session directory exists
  createDir(sessionDir)

  # Open log file in append mode
  let logPath = sessionDir / (contextId & ".log")
  result = Logger(
    file: open(logPath, fmAppend),
    seq: 0
  )

proc log*(logger: Logger, entries: varargs[(string, JsonNode)]) =
  ## Log an entry to the context log file
  ##
  ## Args:
  ##   entries: Key-value pairs to log as tuples

  # Increment global sequence number
  inc globalSeq

  # Format timestamp in human-readable format
  let timestamp = now().format("yyyy-MM-dd HH:mm:ss")

  # Build the log entry
  var lines: seq[string] = @[]
  lines.add("timestamp: " & timestamp)
  lines.add("seq!: " & $globalSeq)

  # Add all entries as key-value pairs
  for (key, valueNode) in entries:
    var entryKey = key
    var value: string

    if valueNode.kind == JString:
      value = valueNode.getStr()
      if value.contains('\n') or value.startsWith("<<<"):
        let delimiter = findHeredocDelimiter(value)
        value = "<<<" & delimiter & "\n" & value & "\n" & delimiter
    else:
      # Non-string types get JSON encoding and ! suffix
      entryKey = key & "!"
      value = $valueNode

    lines.add(entryKey & ": " & value)

  logger.file.write(lines.join("\n") & "\n\n")
  logger.file.flushFile()

proc close*(logger: Logger) =
  ## Close the log file
  logger.file.close()

# Helper procs for common log operations
proc logStr*(logger: Logger, pairs: varargs[(string, string)]) =
  ## Log string key-value pairs
  var jsonPairs: seq[(string, JsonNode)] = @[]
  for (k, v) in pairs:
    jsonPairs.add((k, %v))
  logger.log(jsonPairs)

proc logMixed*(logger: Logger, pairs: openArray[(string, JsonNode)]) =
  ## Log mixed key-value pairs
  logger.log(pairs)
