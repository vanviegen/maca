## Docker/Podman operations for safe command execution in containers

import std/[osproc, os, strutils, tables, sha1]

type
  ContainerError* = object of CatchableError

  ContainerResult* = object
    stdout*: string
    stderr*: string
    exitCode*: int

var
  containerRuntime: string = ""
  imageCache: Table[string, string] = initTable[string, string]()

proc getContainerRuntime*(): string =
  ## Detect and return the available container runtime (podman or docker)
  if containerRuntime != "":
    return containerRuntime

  # Prefer podman for rootless execution
  if findExe("podman") != "":
    containerRuntime = "podman"
  elif findExe("docker") != "":
    containerRuntime = "docker"
  else:
    raise newException(ContainerError, "Neither podman nor docker found in PATH")

  return containerRuntime

proc buildImage*(baseImage: string, dockerRuns: seq[string]): string =
  ## Build a Docker image with the specified RUN commands, or return cached image ID
  let runtime = getContainerRuntime()

  # Create a cache key from base image and runs
  let cacheKey = ($secureHash(baseImage & ":" & dockerRuns.join(":"))).substr(0, 11)

  if imageCache.hasKey(cacheKey):
    return imageCache[cacheKey]

  # Build the Dockerfile content
  var dockerfileContent = "FROM " & baseImage & "\n"
  for runCmd in dockerRuns:
    if runCmd.strip() != "":
      dockerfileContent &= runCmd & "\n"

  # Create a temporary directory for the build context
  let tmpDir = getTempDir() / "maca-build-" & cacheKey
  createDir(tmpDir)

  try:
    let dockerfilePath = tmpDir / "Dockerfile"
    writeFile(dockerfilePath, dockerfileContent)

    # Build the image
    let imageTag = "maca-build-" & cacheKey
    let cmd = runtime & " build -t " & imageTag & " -f " & dockerfilePath & " " & tmpDir

    let (output, exitCode) = execCmdEx(cmd)

    if exitCode != 0:
      raise newException(ContainerError, "Failed to build container image:\n" & output)

    imageCache[cacheKey] = imageTag
    return imageTag

  finally:
    removeDir(tmpDir)

proc truncateOutput*(output: string, head, tail: int): string =
  ## Truncate output to keep only head and tail lines
  let lines = output.splitLines()

  if lines.len <= head + tail:
    return output

  let strippedCount = lines.len - head - tail
  var headLines = lines[0 ..< head]
  var tailLines: seq[string] = @[]
  if tail > 0:
    tailLines = lines[^tail .. ^1]

  result = headLines.join("\n")
  result &= "\n\n... " & $strippedCount & " more lines stripped (change head/tail to see them, or use grep to search for specific output) ...\n\n"
  result &= tailLines.join("\n")

proc runInContainer*(
  command: string,
  worktreePath: string,
  repoRoot: string,
  dockerImage = "debian:stable",
  dockerRuns: seq[string] = @[],
  head = 50,
  tail = 50
): ContainerResult =
  ## Execute a shell command in an ephemeral container
  ##
  ## Args:
  ##   command: The shell command to execute
  ##   worktreePath: Path to the worktree to mount
  ##   repoRoot: Path to the repository root (for .git)
  ##   dockerImage: Base Docker image to use
  ##   dockerRuns: List of RUN commands to execute when building the image
  ##   head: Number of lines to keep from the start of output
  ##   tail: Number of lines to keep from the end of output
  ##
  ## Returns:
  ##   ContainerResult with stdout, stderr (combined and truncated), and exitCode

  let runtime = getContainerRuntime()

  # Build the image if we have RUN commands
  var image = dockerImage
  if dockerRuns.len > 0:
    image = buildImage(dockerImage, dockerRuns)

  # Resolve absolute paths
  let worktreeAbs = expandFilename(worktreePath)
  let repoRootAbs = expandFilename(repoRoot)
  let gitDir = repoRootAbs / ".git"

  # Build the container run command
  let cmd = runtime & " run --rm" &
    " -v " & worktreeAbs & ":" & worktreeAbs &
    " -v " & gitDir & ":" & gitDir & ":ro" &
    " -w " & worktreeAbs &
    " " & image &
    " sh -c '" & command.replace("'", "'\\''") & "'"

  # Execute the command
  let (output, exitCode) = execCmdEx(cmd)

  # For now, combine stdout and stderr (execCmdEx already does this)
  let combinedOutput = output

  # Truncate if necessary
  let truncatedOutput = truncateOutput(combinedOutput, head, tail)

  return ContainerResult(
    stdout: truncatedOutput,
    stderr: "",  # execCmdEx combines them
    exitCode: exitCode
  )
