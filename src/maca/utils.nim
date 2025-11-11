## Utility functions for MACA

import terminal

type
  PrintPart* = object
    case isColored*: bool
    of true:
      color*: ForegroundColor
      text*: string
    of false:
      plainText*: string

proc plain*(text: string): PrintPart =
  ## Create a plain text part
  PrintPart(isColored: false, plainText: text)

proc colored*(color: ForegroundColor, text: string): PrintPart =
  ## Create a colored text part
  PrintPart(isColored: true, color: color, text: text)

proc green*(text: string): PrintPart = colored(fgGreen, text)
proc red*(text: string): PrintPart = colored(fgRed, text)
proc yellow*(text: string): PrintPart = colored(fgYellow, text)
proc cyan*(text: string): PrintPart = colored(fgCyan, text)
proc blue*(text: string): PrintPart = colored(fgBlue, text)
proc magenta*(text: string): PrintPart = colored(fgMagenta, text)

proc colorPrint*(parts: varargs[PrintPart]) =
  ## Print parts with optional color formatting.
  ##
  ## Example:
  ##   colorPrint(green("Hello "), plain("world!"))
  ##   # Prints "Hello world!" where "Hello " is green and "world!" is default color

  for part in parts:
    if part.isColored:
      stdout.styledWrite(part.color, part.text)
    else:
      stdout.write(part.plainText)

  stdout.flushFile()

proc colorPrintLn*(parts: varargs[PrintPart]) =
  ## Same as colorPrint but adds newline at the end
  colorPrint(parts)
  echo ""
