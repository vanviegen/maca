## Context classes for managing different types of LLM interactions

import std/[json, httpclient, os, strutils, times, tables, strformat]
import logger, git_ops, utils
import tools  # Import tools module

type
  ContextError* = object of CatchableError

  Context* = ref object
    contextId*: string
    contextType*: string
    apiKey*: string
    messages*: seq[JsonNode]
    cumulativeCost*: int
    agentsMdContent*: string
    lastHeadCommit*: string
    defaultModel*: string
    model*: string
    toolNames*: seq[string]
    toolSchemas*: seq[JsonNode]
    logger*: Logger
    # References to maca instance data
    repoRoot*: string
    sessionId*: int
    worktreePath*: string

var instanceCounters: Table[string, int] = initTable[string, int]()

proc loadSystemPrompt(ctx: Context, promptsDir: string) =
  ## Load the system prompt from markdown files and parse metadata

  # First load common.md (shared across all contexts)
  let commonPath = promptsDir / "common.md"
  if not fileExists(commonPath):
    raise newException(ContextError, "Common prompt not found: " & commonPath)

  let commonPrompt = readFile(commonPath)
  ctx.messages.add(%*{
    "role": "system",
    "content": commonPrompt
  })

  # Then load context-specific prompt
  let promptPath = promptsDir / (ctx.contextType & ".md")
  if not fileExists(promptPath):
    raise newException(ContextError, "System prompt not found: " & promptPath)

  let promptContent = readFile(promptPath)

  # Split into headers and prompt body on first blank line
  let parts = promptContent.split("\n\n", 1)
  if parts.len != 2:
    raise newException(ContextError, "Prompt file must have headers separated by blank line")

  let headersText = parts[0]
  let systemPrompt = parts[1]

  # Parse headers
  for line in headersText.splitLines():
    let trimmed = line.strip()
    if trimmed == "":
      continue

    if ':' notin trimmed:
      raise newException(ContextError, "Invalid header format: " & line)

    let colonPos = trimmed.find(':')
    let key = trimmed[0 ..< colonPos].strip()
    let value = trimmed[colonPos + 1 .. ^1].strip()

    case key
    of "default_model":
      ctx.defaultModel = value
    of "tools":
      ctx.toolNames = value.split(',').mapIt(it.strip())
    else:
      raise newException(ContextError, "Unknown header key: " & key)

  ctx.messages.add(%*{
    "role": "system",
    "content": systemPrompt
  })

proc loadAgentsMd(ctx: Context) =
  ## Load AGENTS.md from the worktree if it exists
  if ctx.worktreePath == "":
    return

  let agentsPath = ctx.worktreePath / "AGENTS.md"
  if fileExists(agentsPath):
    let content = readFile(agentsPath)
    ctx.agentsMdContent = content
    ctx.messages.add(%*{
      "role": "system",
      "content": "# Project Context (AGENTS.md)\n\n" & content
    })

proc diffAgentsMd(ctx: Context) =
  ## Check if AGENTS.md has been updated and append diff to context
  if ctx.worktreePath == "":
    return

  let agentsPath = ctx.worktreePath / "AGENTS.md"
  if not fileExists(agentsPath):
    return

  let newContent = readFile(agentsPath)

  # Check if content has changed
  if newContent == ctx.agentsMdContent:
    return

  # For simplicity, just add a system message about the update
  # In a full implementation, you could compute an actual diff
  ctx.agentsMdContent = newContent
  ctx.messages.add(%*{
    "role": "system",
    "content": "# AGENTS.md Updated\n\nAGENTS.md has been updated with new content."
  })

proc checkHeadChanges(ctx: Context) =
  ## Check if HEAD has changed since last invocation
  if ctx.worktreePath == "" or ctx.lastHeadCommit == "":
    return

  let currentHead = getHeadCommit(cwd = ctx.worktreePath)

  if currentHead != ctx.lastHeadCommit:
    # HEAD has changed, gather info
    let commits = getCommitsBetween(ctx.lastHeadCommit, currentHead, cwd = ctx.worktreePath)
    let changedFiles = getChangedFilesBetween(ctx.lastHeadCommit, currentHead, cwd = ctx.worktreePath)

    if commits.len > 0 or changedFiles.len > 0:
      # Build system message
      var messageParts = @["# Repository Updates\n\nThe following changes have been made since you were last invoked:\n"]

      if commits.len > 0:
        messageParts.add("\n## New Commits\n")
        for commit in commits:
          messageParts.add(&"- `{commit.hash}` {commit.message}")

      if changedFiles.len > 0:
        messageParts.add("\n\n## Changed Files\n")
        for filepath in changedFiles:
          messageParts.add(&"- {filepath}")

      ctx.messages.add(%*{
        "role": "system",
        "content": messageParts.join("\n")
      })

    # Update tracking
    ctx.lastHeadCommit = currentHead

proc newContext*(
  contextType: string,
  repoRoot: string,
  sessionId: int,
  worktreePath: string,
  model = "auto",
  contextId = "",
  initialMessage = ""
): Context =
  ## Initialize a context
  ##
  ## Args:
  ##   contextType: Type of context (main, code_analysis, research, etc.)
  ##   repoRoot: Repository root path
  ##   sessionId: Session ID number
  ##   worktreePath: Path to the worktree
  ##   model: Model to use ("auto" to use default from prompt)
  ##   contextId: Optional context ID (auto-generated if not provided)
  ##   initialMessage: Optional initial user message

  var finalContextId = contextId
  if finalContextId == "":
    # Auto-generate unique name
    if not instanceCounters.hasKey(contextType):
      instanceCounters[contextType] = 0
    instanceCounters[contextType] += 1
    finalContextId = contextType & $instanceCounters[contextType]

  result = Context(
    contextId: finalContextId,
    contextType: contextType,
    repoRoot: repoRoot,
    sessionId: sessionId,
    worktreePath: worktreePath,
    messages: @[],
    cumulativeCost: 0,
    defaultModel: "openai/gpt-5-mini",
    toolNames: @[]
  )

  # Get API key from environment
  result.apiKey = getEnv("OPENROUTER_API_KEY")
  if result.apiKey == "":
    raise newException(ContextError, "OPENROUTER_API_KEY not set")

  # Initialize logger
  result.logger = newLogger(repoRoot, sessionId, finalContextId)

  # Load system prompt and parse metadata
  let scriptDir = getAppDir()
  let promptsDir = scriptDir / "prompts"
  result.loadSystemPrompt(promptsDir)

  # Get tool schemas
  result.toolSchemas = getToolSchemas(result.toolNames, addRationale = (contextType != "_main"))

  # Set model
  if model == "auto":
    result.model = result.defaultModel
  else:
    result.model = model

  # Load AGENTS.md if it exists
  result.loadAgentsMd()

  # Add unique name info
  result.messages.add(%*{
    "role": "system",
    "content": "Your unique context identifier is: **" & finalContextId & "**"
  })

  # Initialize HEAD tracking
  if worktreePath != "":
    result.lastHeadCommit = getHeadCommit(cwd = worktreePath)

  # Add initial message if provided
  if initialMessage != "":
    result.messages.add(%*{
      "role": "user",
      "content": initialMessage
    })

proc addMessage*(ctx: Context, message: JsonNode) =
  ## Add a message to the context and log it
  ctx.logger.log([("tag", %"message"), ("content", message)])
  ctx.messages.add(message)

proc callLlm*(ctx: Context): tuple[message: JsonNode, cost: int] =
  ## Call the LLM and return the response
  ##
  ## Returns tuple with message and cost in microdollars

  let client = newHttpClient()
  client.headers = newHttpHeaders({
    "Content-Type": "application/json",
    "Authorization": "Bearer " & ctx.apiKey,
    "HTTP-Referer": "https://github.com/vanviegen/maca",
    "X-Title": "MACA - Multi-Agent Coding Assistant"
  })

  let requestBody = %*{
    "model": ctx.model,
    "messages": ctx.messages,
    "tools": ctx.toolSchemas,
    "usage": {"include": true},
    "tool_choice": "required"
  }

  let startTime = epochTime()

  try:
    let response = client.postContent(
      "https://openrouter.ai/api/v1/chat/completions",
      body = $requestBody
    )

    let responseJson = parseJson(response)
    let choice = responseJson["choices"][0]
    let message = choice["message"]

    # Extract usage
    let usage = responseJson["usage"]
    let cost = int(usage["cost"].getFloat() * 1_000_000)  # Convert to microdollars
    ctx.cumulativeCost += cost

    let duration = epochTime() - startTime

    ctx.logger.log([
      ("tag", %"llm_call"),
      ("model", %ctx.model),
      ("cost", %cost),
      ("prompt_tokens", usage["prompt_tokens"]),
      ("completion_tokens", usage["completion_tokens"]),
      ("duration", %duration)
    ])

    # Add assistant message to history
    ctx.addMessage(message)

    return (message, cost)

  except Exception as e:
    raise newException(ContextError, "LLM API error: " & e.msg)
  finally:
    client.close()

proc run*(ctx: Context, budget = 0): tuple[summary: string, completed: bool, cost: int] =
  ## Run this context until completion or budget exceeded
  ##
  ## Args:
  ##   budget: Maximum cost in microdollars (0 = unlimited)
  ##
  ## Returns:
  ##   Tuple with summary, completion status, and total cost

  var totalCost = 0
  var completed = false
  var summaryParts: seq[string] = @[]
  let isSubcontext = ctx.contextType != "_main"
  let indent = if isSubcontext: "  " else: ""

  # Main loop
  while not completed:
    colorPrintLn(cyan(indent & "Context '" & ctx.contextId & "' thinking..."))

    # Check for AGENTS.md updates
    ctx.diffAgentsMd()

    # Check for HEAD changes
    ctx.checkHeadChanges()

    # Call LLM with retry logic
    var llmResult: tuple[message: JsonNode, cost: int]
    var success = false

    for attempt in 0..2:
      try:
        llmResult = ctx.callLlm()
        success = true
        break
      except Exception as e:
        colorPrintLn(red(indent & "Error during LLM call: " & e.msg & ". Retrying..."))
        summaryParts.add("Error during LLM call: " & e.msg)

    if not success:
      break

    let message = llmResult.message
    let cost = llmResult.cost
    totalCost += cost

    # Extract tool calls
    if not message.hasKey("tool_calls"):
      raise newException(ContextError, "No tool calls in response")

    let toolCalls = message["tool_calls"].getElems()
    if toolCalls.len != 1:
      raise newException(ContextError, "Expected exactly 1 tool call, got " & $toolCalls.len)

    let toolCall = toolCalls[0]
    let toolName = toolCall["function"]["name"].getStr()
    let toolArgs = parseJson(toolCall["function"]["arguments"].getStr())

    # Extract rationale if present
    var rationale = ""
    if toolArgs.hasKey("rationale"):
      rationale = toolArgs["rationale"].getStr()

    # Log tool call
    ctx.logger.log([("tag", %"tool_call"), ("tool", %toolName), ("args", toolArgs)])

    # Print tool info
    colorPrint(green(indent & "→"), plain(" Tool: "), yellow(toolName))
    if rationale != "":
      colorPrintLn(plain(""))
      colorPrintLn(plain(indent & "  Rationale: " & rationale))
    else:
      colorPrintLn(plain(""))

    # Execute tool
    let startTime = epochTime()
    var toolResult: JsonNode
    var toolError = false

    try:
      # Create tool context
      let toolCtx = ToolContext(
        worktreePath: ctx.worktreePath,
        repoRoot: ctx.repoRoot,
        sessionId: ctx.sessionId
      )

      toolResult = executeTool(toolName, toolArgs, toolCtx)

    except Exception as e:
      toolError = true
      toolResult = %*{"error": e.msg}
      colorPrintLn(red(indent & "Tool error: " & e.msg))

    let toolDuration = epochTime() - startTime

    # Check if this is a ready result
    var isReady = false
    if toolResult.hasKey("ready") and toolResult["ready"].getBool():
      isReady = true
      completed = true

    ctx.logger.log([
      ("tag", %"tool_result"),
      ("tool", %toolName),
      ("duration", %toolDuration),
      ("result", toolResult),
      ("completed", %completed)
    ])

    # Add tool result to messages
    ctx.messages.add(%*{
      "role": "tool",
      "tool_call_id": toolCall["id"],
      "content": $toolResult
    })

    # Check for git changes and commit if needed
    if not toolError and ctx.worktreePath != "":
      let diffStats = getDiffStats(ctx.worktreePath)
      if diffStats != "":
        # Commit changes
        let commitMsg = if rationale != "": &"{toolName}: {rationale}" else: toolName
        discard commitChanges(ctx.worktreePath, commitMsg)
        ctx.logger.log([("tag", %"commit"), ("message", %commitMsg), ("diff_stats", %diffStats)])
        colorPrintLn(green(indent & "✓ Committed changes"))

    # Build summary for this iteration
    var abbrArgs = $toolArgs
    if abbrArgs.len > 120:
      abbrArgs = abbrArgs[0..79] & "..."
    let iterationSummary = &"Called {toolName}({abbrArgs}) because: {rationale}"
    summaryParts.add(iterationSummary)

    if completed:
      colorPrintLn(green(indent & &"✓ Context {ctx.contextId} completed. Cost: {totalCost}μ$"))
      ctx.logger.log([("tag", %"complete")])
      if toolResult.hasKey("message"):
        summaryParts.add(toolResult["message"].getStr())
      break

    # Check budget (only for subcontexts)
    if budget > 0 and totalCost > budget:
      let budgetMsg = &"Context '{ctx.contextId}' budget exceeded (spent {totalCost}μ$ of {budget}μ$)"
      colorPrintLn(yellow(indent & budgetMsg))
      summaryParts.add(budgetMsg)
      break

  result = (summaryParts.join("\n"), completed, totalCost)
