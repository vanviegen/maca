## Git operations for agentic coding assistant worktree management

import std/[osproc, os, strutils, re, sequtils]

type
  GitError* = object of CatchableError

  CommitInfo* = object
    hash*: string
    message*: string

proc runGit*(args: varargs[string], cwd = "", check = true): tuple[output: string, exitCode: int] =
  ## Run a git command and return the result
  let cmd = "git " & args.join(" ")
  let workDir = if cwd == "": getCurrentDir() else: cwd

  let (output, exitCode) = execCmdEx(cmd, workingDir = workDir)

  if check and exitCode != 0:
    raise newException(GitError, "Git command failed: " & cmd & "\n" & output)

  return (output.strip(), exitCode)

proc isGitRepo*(path = "."): bool =
  ## Check if the given path is inside a git repository
  let (_, exitCode) = runGit(["rev-parse", "--git-dir"], cwd = path, check = false)
  return exitCode == 0

proc initGitRepo*(path = ".") =
  ## Initialize a new git repository
  discard runGit(["init"], cwd = path)

  # Create an initial commit to have a main branch
  let readmePath = path / "README.md"
  if not fileExists(readmePath):
    writeFile(readmePath, "# Project\n\nInitialized by maca.\n")

  discard runGit(["add", "README.md"], cwd = path)
  discard runGit(["commit", "-m", "\"Initial commit\""], cwd = path)

proc getRepoRoot*(path = "."): string =
  ## Get the root directory of the git repository
  let (output, _) = runGit(["rev-parse", "--show-toplevel"], cwd = path)
  return output

proc getCurrentBranch*(cwd = "."): string =
  ## Get the name of the current branch
  let (output, _) = runGit(["rev-parse", "--abbrev-ref", "HEAD"], cwd = cwd)
  return output

proc getHeadCommit*(cwd = "."): string =
  ## Get the current HEAD commit hash
  let (output, _) = runGit(["rev-parse", "HEAD"], cwd = cwd)
  return output

proc getCommitsBetween*(oldCommit, newCommit: string, cwd = "."): seq[CommitInfo] =
  ## Get list of commits between oldCommit and newCommit
  ##
  ## Returns seq of CommitInfo with 'hash' and 'message' (first line only)

  let (output, exitCode) = runGit(["log", "--format=%H %s", oldCommit & ".." & newCommit], cwd = cwd, check = false)

  if exitCode != 0 or output == "":
    return @[]

  result = @[]
  for line in output.splitLines():
    if line != "":
      let parts = line.split(' ', 1)
      if parts.len == 2:
        result.add(CommitInfo(
          hash: parts[0][0..7],  # Short hash
          message: parts[1]
        ))

proc getChangedFilesBetween*(oldCommit, newCommit: string, cwd = "."): seq[string] =
  ## Get list of files changed between oldCommit and newCommit

  let (output, exitCode) = runGit(["diff", "--name-only", oldCommit, newCommit], cwd = cwd, check = false)

  if exitCode != 0 or output == "":
    return @[]

  return output.splitLines().filterIt(it != "")

proc findNextSessionId*(repoRoot: string): int =
  ## Find the next available session ID by checking .maca directory
  let macaDir = repoRoot / ".maca"
  createDir(macaDir)

  # Find all existing session directories
  var existing: seq[int] = @[]
  for kind, path in walkDir(macaDir):
    if kind == pcDir:
      let name = path.lastPathPart()
      try:
        existing.add(parseInt(name))
      except ValueError:
        discard

  if existing.len > 0:
    return max(existing) + 1
  else:
    return 1

proc createSessionWorktree*(repoRoot: string, sessionId: int): tuple[worktreePath: string, branchName: string] =
  ## Create a new branch and worktree for the session
  let branchName = "maca-" & $sessionId
  let sessionDir = repoRoot / ".maca" / $sessionId
  let worktreePath = sessionDir / "worktree"

  # Ensure session directory exists
  createDir(sessionDir)

  # Get current branch to branch from
  let currentBranch = getCurrentBranch(cwd = repoRoot)

  # Clean up stale worktrees and branches
  discard runGit(["worktree", "prune"], cwd = repoRoot)

  # Create new branch
  discard runGit(["branch", "-f", branchName, currentBranch], cwd = repoRoot)

  # Create worktree
  discard runGit(["worktree", "add", worktreePath, branchName], cwd = repoRoot)

  # Create .scratch directory for temporary analysis files
  let scratchDir = worktreePath / ".scratch"
  createDir(scratchDir)

  return (worktreePath, branchName)

proc commitChanges*(worktreePath: string, message: string): bool =
  ## Commit all changes in the worktree with the given message, excluding .scratch and .maca

  # Add all changes (including untracked files), but exclude .scratch and .maca
  discard runGit(["add", "-A", ":!.scratch", ":!.maca"], cwd = worktreePath)

  # Check if there are changes to commit
  let (_, exitCode) = runGit(["diff", "--cached", "--quiet"], cwd = worktreePath, check = false)
  if exitCode == 0:
    # No changes to commit
    return false

  # Commit
  discard runGit(["commit", "-m", "\"" & message.replace("\"", "\\\"") & "\""], cwd = worktreePath)
  return true

proc getDiffStats*(worktreePath: string): string =
  ## Get the diff statistics for uncommitted changes
  let (output, _) = runGit(["diff", "--numstat", "HEAD"], cwd = worktreePath, check = false)
  return output

proc generateDescriptiveBranchName*(commitMessage, repoRoot: string): string =
  ## Generate a descriptive branch name from commit message
  ##
  ## Returns a branch name that doesn't conflict with existing maca/* branches

  # Extract first line of commit message
  var firstLine = commitMessage.splitLines()[0].strip()

  # Remove common prefixes
  for prefix in ["Add", "Update", "Fix", "Remove", "Refactor", "Implement"]:
    if firstLine.startsWith(prefix & " "):
      firstLine = firstLine[prefix.len + 1..^1]
      break

  # Convert to branch name format (lowercase, hyphenated, max 40 chars)
  var name = firstLine.toLowerAscii()
  name = name.replace(re"[^a-z0-9\s-]", "")  # Remove special chars
  name = name.replace(re"\s+", "-")  # Replace spaces with hyphens
  name = name.replace(re"-+", "-")  # Collapse multiple hyphens
  name = name.strip(chars = {'-'})  # Remove leading/trailing hyphens
  if name.len > 40:
    name = name[0..39]
  name = name.strip(trailing = true, chars = {'-'})  # Remove trailing hyphen if truncated

  # Ensure we have something
  if name == "":
    name = "changes"

  # Check if maca/<name> exists, if so add suffix
  let baseName = name
  var counter = 2
  while true:
    let fullBranch = "maca/" & name
    let (_, exitCode) = runGit(["rev-parse", "--verify", fullBranch], cwd = repoRoot, check = false)
    if exitCode != 0:
      # Branch doesn't exist, we can use it
      break
    # Branch exists, try next variant
    name = baseName & "-" & $counter
    inc counter

  return name

proc mergeToMain*(repoRoot, worktreePath, branchName, commitMessage: string): tuple[success: bool, message: string] =
  ## Merge the session branch into main using squash + rebase + ff strategy
  let mainBranch = getCurrentBranch(cwd = repoRoot)

  # Switch to the session branch in the main repo
  discard runGit(["checkout", branchName], cwd = repoRoot)

  # Save the current HEAD commit hash (before squashing)
  let (originalHead, _) = runGit(["rev-parse", "HEAD"], cwd = repoRoot)

  # Get the merge base
  let (baseCommit, _) = runGit(["merge-base", mainBranch, branchName], cwd = repoRoot)

  # Generate descriptive branch name for preserving history
  let descriptiveName = generateDescriptiveBranchName(commitMessage, repoRoot)

  # Append preservation note to commit message
  var enhancedMessage = commitMessage
  if not enhancedMessage.endsWith("\n"):
    enhancedMessage &= "\n"
  enhancedMessage &= "\nThe original chain of MACA commits is kept in the maca/" & descriptiveName & " branch."

  # Soft reset to base
  discard runGit(["reset", "--soft", baseCommit], cwd = repoRoot)

  # Commit everything as one commit with enhanced message
  discard runGit(["commit", "-m", "\"" & enhancedMessage.replace("\"", "\\\"") & "\""], cwd = repoRoot, check = false)

  # Go back to main branch
  discard runGit(["checkout", mainBranch], cwd = repoRoot)

  # Try to rebase the session branch onto main
  let (_, rebaseExitCode) = runGit(["rebase", mainBranch, branchName], cwd = repoRoot, check = false)

  if rebaseExitCode != 0:
    # Rebase failed, likely due to conflicts
    # Abort the rebase
    discard runGit(["rebase", "--abort"], cwd = repoRoot, check = false)
    return (false, "Merge conflicts detected")

  # Fast-forward merge
  discard runGit(["merge", "--ff-only", branchName], cwd = repoRoot)

  # Create the descriptive branch pointing at original HEAD
  discard runGit(["branch", "maca/" & descriptiveName, originalHead], cwd = repoRoot)

  return (true, "Merged successfully")

proc cleanupSession*(repoRoot, worktreePath, branchName: string) =
  ## Clean up the worktree and branch after merge

  # Remove worktree
  discard runGit(["worktree", "remove", worktreePath], cwd = repoRoot, check = false)

  # Delete branch
  discard runGit(["branch", "-D", branchName], cwd = repoRoot, check = false)
