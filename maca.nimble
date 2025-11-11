# Package
version       = "0.1.0"
author        = "MACA Contributors"
description   = "Multi-Agent Coding Assistant - A multi-agent system for orchestrating AI coding tasks"
license       = "MIT"
srcDir        = "src"
bin           = @["maca"]
binDir        = "."

# Dependencies
requires "nim >= 2.0.0"

# Note: The following optional dependencies can enhance functionality:
# - nimline >= 0.1.0  # For better terminal readline interface
# - jsony >= 1.1.5    # For faster JSON parsing
# Install with: nimble install nimline jsony

# Tasks
task build, "Build the MACA binary":
  exec "nim c -d:release -o:maca src/maca.nim"

task debug, "Build with debug symbols":
  exec "nim c --debugger:native -o:maca_debug src/maca.nim"

task clean, "Clean build artifacts":
  exec "rm -f maca maca_debug"
