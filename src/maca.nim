## Multi-Agent Coding Assistant - Main entry point

import std/[os, strutils, tables]
import maca/[git_ops, context, tools, utils, logger]

type
  MACA* = ref object
    initialPrompt*: string
    repoPath*: string
    repoRoot*: string
    sessionId*: int
    worktreePath*: string
    branchName*: string
    mainContext*: Context
    subcontexts*: Table[string, Context]
    contextCounters*: Table[string, int]

proc newMACA*(): MACA =
  ## Create a new MACA instance
  result = MACA(
    subcontexts: initTable[string, Context](),
    contextCounters: initTable[string, int]()
  )

proc ensureGitRepo*(maca: MACA) =
  ## Ensure we're in a git repository, or offer to initialize one
  if not isGitRepo(maca.repoPath):
    echo "Not in a git repository."
    echo "MACA requires a git repository. Initialize one now? (yes/no)"
    let response = readLine(stdin).toLowerAscii()

    if response != "yes" and response != "y":
      echo "Exiting."
      quit(0)

    initGitRepo(maca.repoPath)
    colorPrintLn(green("Git repository initialized."))

  maca.repoRoot = getRepoRoot(maca.repoPath)

proc createSession*(maca: MACA) =
  ## Create a new session with worktree and branch
  # Find next session ID
  maca.sessionId = findNextSessionId(maca.repoRoot)

  # Create worktree and branch
  let (worktree, branch) = createSessionWorktree(maca.repoRoot, maca.sessionId)
  maca.worktreePath = worktree
  maca.branchName = branch

  colorPrintLn(
    green("Session " & $maca.sessionId & " created"),
    plain(" (branch: "),
    cyan(maca.branchName),
    plain(", worktree: "),
    cyan(maca.worktreePath.relativePath(maca.repoRoot)),
    plain(")")
  )

proc run*(maca: MACA, directory, task, model: string) =
  ## Run MACA with the given parameters
  maca.initialPrompt = task
  maca.repoPath = expandFilename(directory)
  maca.ensureGitRepo()

  # Create session
  maca.createSession()

  # Initialize tools
  initTools()

  # Initialize main context
  let modelToUse = if model != "": model else: "auto"
  maca.mainContext = newContext(
    contextType = "_main",
    repoRoot = maca.repoRoot,
    sessionId = maca.sessionId,
    worktreePath = maca.worktreePath,
    model = modelToUse,
    contextId = "main"
  )

  # TODO: Auto-call list_files for top-level directory

  # Main loop
  while true:
    # Get initial prompt if this is a new task
    var prompt = maca.initialPrompt
    if prompt != "":
      maca.initialPrompt = ""  # Only use once
    else:
      colorPrintLn(yellow("Enter your task (Ctrl+D to submit):"))
      # Simple multi-line input
      var lines: seq[string] = @[]
      try:
        while true:
          let line = readLine(stdin)
          lines.add(line)
      except EOFError:
        discard
      prompt = lines.join("\n").strip()

    if prompt != "":
      maca.mainContext.addMessage(%*{
        "role": "user",
        "content": prompt
      })
      discard maca.mainContext.run()

# CLI argument parsing and entry point
when isMainModule:
  import parseopt

  var
    task = ""
    model = "anthropic/claude-sonnet-4.5"
    directory = "."

  # Parse command line arguments
  var p = initOptParser()
  while true:
    p.next()
    case p.kind
    of cmdEnd: break
    of cmdShortOption, cmdLongOption:
      case p.key
      of "m", "model":
        model = p.val
      of "d", "directory":
        directory = p.val
      of "h", "help":
        echo """
Multi-Agent Coding Assistant (MACA)

Usage:
  maca [options] [task]

Options:
  -m, --model MODEL       Model to use for main context (default: anthropic/claude-sonnet-4.5)
  -d, --directory DIR     Project directory (default: current directory)
  -h, --help             Show this help message

Examples:
  maca                           # Run interactively
  maca "implement feature X"     # Run with initial task
  maca -m openai/gpt-4 "task"   # Use specific model
"""
        quit(0)
      else:
        echo "Unknown option: ", p.key
        quit(1)
    of cmdArgument:
      if task != "":
        task &= " "
      task &= p.key

  # Run MACA
  let maca = newMACA()
  maca.run(directory, task, model)
