#!/usr/bin/env python3
import subprocess
import sys
import os
import re
import argparse
import json
import urllib
import urllib.request
import glob
from pathlib import Path

import pyperclip
import prompt_toolkit

ALIASES = {
    "sonnet": "anthropic/claude-sonnet-4.5",
    "s": "anthropic/claude-sonnet-4.5",
    "qwen": "qwen/qwen3-vl-8b-instruct",
    "q": "qwen/qwen3-vl-8b-instruct",
    "gpt": "openai/gpt-5",
    "gemini": "google/gemini-2.5-pro",
    "g": "google/gemini-2.5-pro",
    "gemini-flash-lite": "google/gemini-2.5-flash-lite",
    "gfl": "google/gemini-2.5-flash-lite",
    "z": "z-ai/glm-4.6",
}

LOG_DIR = Path.home() / '.local/state/ai'
LOG_MAX_SIZE = 512 * 1024

# Initialize log files
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILES = sorted(glob.glob(str(LOG_DIR / '*.log')), key=lambda p: int(re.search(r'(\d+)\.log$', p).group(1)))

# Cleanup old logs - keep only 10 most recent
for f in LOG_FILES[:-10]:
    os.remove(f)
LOG_FILES = LOG_FILES[-10:]

# Determine current log file
if LOG_FILES and os.path.getsize(LOG_FILES[-1]) < LOG_MAX_SIZE:
    LOG_FILE = LOG_FILES[-1]
else:
    num = int(re.search(r'(\d+)\.log$', LOG_FILES[-1]).group(1)) + 1 if LOG_FILES else 1
    LOG_FILE = str(LOG_DIR / f'{num}.log')
    LOG_FILES.append(LOG_FILE)

total_cost = 0.0

def write_log(messages, assistant, model, usage):
    """Append request and response to log file."""
    with open(LOG_FILE, 'a') as f:
        for msg in messages:
            f.write(f"-~={msg['role']}=~-\n{msg['content']}\n")
        f.write(f"-~=meta=~-\nmodel: {model}\n")
        if usage:
            global total_cost
            total_cost += usage.get('cost', 0)
            for what in ['prompt_tokens', 'completion_tokens', 'cost']:
                if what in usage:
                    f.write(f"{what}: {usage[what]}\n")
        f.write(f"-~=assistant=~-\n{assistant}\n-~=end=~-\n\n\n\n")

def read_history(count=1):
    """Read the Nth last conversation from logs as a list of messages."""
    for path in reversed(LOG_FILES):
        with open(path) as f:
            content = f.read()
        
        items = re.split(r'\n-~=end=~-', content)[:-1] # Last split is empty

        if count <= len(items):
            item = items[-count]
            messages = []
            for line in ("\n"+item).split('\n-~=')[1:]:
                role, content = line.split('=~-\n', 1)
                if role == 'meta':
                    continue
                messages.append({"role": role, "content": content})
            return messages
        
        count -= len(items)
    
    print("No such item in history")
    exit(1)

def request(url, data, headers):
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print("\nLLM error:", json.loads(e.read().decode('utf-8')))
        exit(1)

parser = argparse.ArgumentParser(
    prog="ai",
    description="Ask an LLM for help on the command line.",
    epilog="Additional query arguments can be passed anywhere in the command. These arguments will be used as the query. If there is no query, the user is prompted.",
)
parser.add_argument('-c', '--continue', dest='_continue', nargs='?', const=1, type=int, metavar='N', help="Continue the Nth previous conversation (default: 1).")
parser.add_argument('-e', '--edit', nargs='+', default=[], help="Have the LLM edit this file. Can be used multiple times.")
parser.add_argument('-f', '--file', nargs='+', default=[], help="Pass file(s) to the LLM. Can be used multiple times.")
parser.add_argument('-k', '--key', type=str, help="Set the OpenRouter API key. Default to using OPENROUTER_API_KEY environment variable.")
parser.add_argument('-l', '--last', nargs='?', const=1, type=int, metavar='N', help="Show the Nth last response again (default: 1).")
parser.add_argument('-m', '--model', type=str, default='sonnet', help="Set the model name (default: 'sonnet').")
parser.add_argument('-r', '--repeat', nargs='?', const=1, type=int, metavar='N', help="Repeat the Nth previous query (default: 1).")

args, query = parser.parse_known_args()

# Get a prompt from the user
if len(query) > 0 or args.repeat or args.last or not sys.stdin.isatty():
    prompt = " ".join(query) 
    # Add stdin data to the prompt
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if prompt:
            prompt += f"\n\n```\n{data.rstrip()}\n```"
        else:
            prompt = data
else:
    print("Type query and press alt-enter:")
    prompt = prompt_toolkit.prompt("> ", prompt_continuation="> ", multiline=True).rstrip()

if not prompt and not args.repeat and not args.last:
    exit()

SEARCH = "<<<<<<< SEARCH"

# Compose the list off messages
if args.repeat or args._continue or args.last:
    messages = read_history(max(args.repeat or 0, args._continue or 0, args.last or 0))
    if not messages:
        print("No history found", file=sys.stderr)
        exit(1)
    if args.repeat:
        messages = messages[:-1]  # Remove assistant response to repeat the query
elif args.edit:
    messages = [
        {"role": "system", "content": f"""You are a non-agentic coding assistant. The user will provide you with a prompt describing what should be done, and an input file for you to edit. Your output should consists of your analysis of the problem and notes on what steps you will take. This part of your output will be ignored, it's only to help your own thinking, so be brief, capture your thoughts in a bulleted list, with little words. If you encounter an error/imperfection in your earlier reasoning, you may correct yourself in later bullet points. Don't summarize at the end, just end with the last bullet point.

After the analysis (the bullet list) you should do one of these three singles:

1. Output a line consisting of only three dashes (---) followed by the full output file. If you choose this option, make sure you output the full file, including any parts that are unchanged. Prefer this option if there are edits scattered throughout most of the file.

or
               
2. Output a line consisting of only three dashes (---) followed by one or more "{SEARCH}\n" <old code> "=======\n" <new code> ">>>>>>>\n". Make sure both <old code> and <new code> blocks contain only full lines, including the newline character. Make very sure the <old code> block exactly matches the corresponding part of the input file, otherwise the patch will not apply. If there's a change the <old code> block is not unique within the file, expand the block with additional (unchanged) lines until it is unique (adding the corresponding newlines to the <new block> as well).

or

3. Stop output immediately (no dashes!), if you conclude in your analysis that the input file already adheres to the user's instructions, or if you conclude that the instructions do not make sense.

Example output:
```
- Goal: fix spelling mistakes and unclear wording in inline documentation
- Spotted several issues to fix
- Changes are local and spawn only a fraction of the file => provide replacements
---
{SEARCH}
 * are processed asynchronously in a batch after a brief timeout (0ms). This function
 * allows you to bypass the timeout and process the update queue immediately.
 *
 * This can be usefull in specific scenarios where you need the DOM to be updated
 * synchronously.
 *
 * This function is re-entrant, meaning it is safe to call `runQueue` from within
=======
 * are processed asynchronously in a batch after a brief timeout (0ms). This function
 * allows you to bypass the timeout and process the update queue immediately.
 *
 * This can be useful in specific scenarios where you need the DOM to be updated
 * synchronously.
 *
 * This function is re-entrant, meaning it is safe to call `runQueue` from within
>>>>>>> REPLACE
{SEARCH}
	sortedSet: ReverseSortedSet<OnEachItemScope, "sortKey"> =
		new ReverseSortedSet("sortKey");

	/** Indexes that has been created/removed and need to be handled in the next `queueRun`. */
	changedIndexes: Set<any> = new Set();

	constructor(
=======
	sortedSet: ReverseSortedSet<OnEachItemScope, "sortKey"> =
		new ReverseSortedSet("sortKey");

	/** Indexes that have been created/removed and need to be handled in the next `queueRun`. */
	changedIndexes: Set<any> = new Set();

	constructor(
>>>>>>> REPLACE
{SEARCH}
export function proxy<T extends any>(target: T): ValueRef<T extends number ? number : T extends string ? string : T extends boolean ? boolean : T>;

/**
 * Creates a reactive proxy around the.
 *
 * Reading properties from the returned proxy within a reactive scope (like one created by
 * {{@link A}} or {{@link derive}}) establishes a subscription. Modifying properties *through*
 * the proxy will notify subscribed scopes, causing them to go.
 *
 * - Plain objects and arrays are wrapped in a standard JavaScript `Proxy` that intercepts
 *   property access and mutations, but otherwise works like the underlying data.
=======
export function proxy<T extends any>(target: T): ValueRef<T extends number ? number : T extends string ? string : T extends boolean ? boolean : T>;

/**
 * Creates a reactive proxy around the given data.
 *
 * Reading properties from the returned proxy within a reactive scope (like one created by
 * {{@link A}} or {{@link derive}}) establishes a subscription. Modifying properties *through*
 * the proxy will notify subscribed scopes, causing them to re-execute.
 *
 * - Plain objects and arrays are wrapped in a standard JavaScript `Proxy` that intercepts
 *   property access and mutations, but otherwise works like the underlying data.
>>>>>>> REPLACE
```

Example output:
```
- Need file watching without external deps - use polling since no built-in inotify in Python stdlib
- `tail -f` replacement: open file, seek to end, read new lines continuously
- Signal handling not needed - Ctrl+C naturally raises KeyboardInterrupt
- Main loop: check file exists → tail it until gone → repeat
- Track file position and inode to detect deletion/recreation
---
#!/usr/bin/env python3

import sys
import os
import time

if len(sys.argv) != 2:
    print("Continuously tail a file even when it's deleted and recreated")
    print(f"Usage: {sys.argv[0]} <file_path>", file=sys.stderr)
    sys.exit(1)

FILE_PATH = sys.argv[1]

try:
    while True:
        if os.path.isfile(FILE_PATH):
            with open(FILE_PATH, 'r') as f:
                # Start from end
                f.seek(0, os.SEEK_END)
                inode = os.fstat(f.fileno()).st_ino
                
                while True:
                    line = f.readline()
                    if line:
                        print(line, end='')
                    else:
                        # Check if file still exists with same inode
                        try:
                            if not os.path.exists(FILE_PATH) or \
                               os.stat(FILE_PATH).st_ino != inode:
                                break
                        except OSError:
                            break
                        time.sleep(0.1)
                
            print("----------------------------------")
        
        time.sleep(0.1)

except KeyboardInterrupt:
    sys.exit(0)
```
         
Example output:
```
- The user wants me to convert the script to Python
- The script is already written in Python
- No changes needed
```

**IMPORTANT**:

- After the separator line (---) you should ONLY output the SEARCH/REPLACE blocks or the full output file (WITHOUT a surrounding Markdown code block).
- If there are edits scattered throughout most of the file, DO NOT use the SEARCH/REPLACE format, but OUTPUT THE FULL FILE instead. It is less error-prone and more efficient.
- So just to be clear, after your analysis bullet points, you have three options:
  1. Output a '---' line followed by SEARCH/REPLACE blocks.
  2. Output a '---' line followed by the full output file. (Preferred, unless only a small fraction of the file needs changes.)
  3. Stop output, if no changes are needed.
- Do NOT add any explanations or notes after the separator line, except of course for comments that belong in the code.
- Do NOT remove any parts of the input file, in particular do NOT remove any comments, unless doing so follows from the user's instructions.
- Do NOT reformat/cleanup code unless doing so is explicitly part of the user's instructions.
"""},
    ]
else:
    messages = [
        {"role": "system", "content": "You are a command-line virtual assistant. The user can ask you questions about any topic, to which you will provide a short but precise answer. If the user asks you to write code, make sure to put the output in a code block with the proper language marker. Assume `fish` as my shell. Python can be used for cases where shell scripting is impractical. Don't repeat the question in your reply. Don't summarize at the end of your reply. Just a code block is fine, if the code speaks for itself."},
    ]

        
model = ALIASES.get(args.model, args.model)


def run_completion(messages):
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {args.key or os.environ["OPENROUTER_API_KEY"]}',
        'HTTP-Referer': 'https://github.com/vanviegen/dotfiles',
        'X-Title': 'AI CLI Tool'
    }
    data = {
        'model': model,
        'messages': messages,
        'usage': {
            'include': True
        }
    }
    
    response = request("https://openrouter.ai/api/v1/chat/completions", data, headers)
    content = response['choices'][0]['message']['content']
    write_log(messages, content, model, response.get('usage'))
    return content


if args.last:
    text = messages[-1]["content"]  # Last message is the assistant response
else:
    # Add new prompt(s) to the message lists
    for filename in args.file:
        with open(filename) as f:
            messages.append({"role": "user", "content": f"{filename}:\n```\n{f.read().rstrip()}\n```"})
    if prompt:
        messages.append({"role": "user", "content": prompt})

    if args.edit:        
        for file in args.edit:
            copy = messages.copy()
            with open(file) as f:
                inData = f.read()

            copy.append({"role": "user", "content": f"My following message is the input file '{file}' you should work on."})
            copy.append({"role": "user", "content": inData.rstrip("\n")})

            for attempt in range(3):
                print(f"Processing '{file}' using {model}...")
                out = run_completion(copy)
                split = out.split("\n---\n", 1)

                # Strip empty lines and ensure all lines start with a dash
                analysis = '\n'.join([f'- {line}' if not line.startswith((' ', '-')) else line for line in split[0].splitlines() if line.strip()])
                print(analysis)

                print(f"* Total cost: m${total_cost*1000:.0f}")

                if len(split) != 2:
                    print("* Skipping")
                    outData = None
                    break
                delta = split[1]

                if "<<<<<<" not in delta:
                    if delta.startswith("```") and delta.rstrip().endswith("```"):
                        print("* Stripping surrounding code block")
                        delta = re.sub(r"^```[a-zA-Z0-9]*\n", "", delta)
                        delta = re.sub(r"\n```$", "", delta)
                    outData = delta
                    print(f"* Replaced full file ({inData.count('\n')+1} -> {outData.count('\n')+1} lines)")
                    break

                # Apply patch
                outData = inData
                err = False
                def repl(m):
                    global outData, err
                    a = m.group(1)
                    b = m.group(2)
                    cnt = outData.count(a)
                    if cnt == 0:
                        print(f"* ERROR: Could not find chunk to replace:\n\n{a}\n")
                        err = True
                    elif cnt > 1:
                        print(f"* ERROR: Chunk to replace is not unique ({cnt} matches):\n\n{a}\n")
                        err = True
                    else:
                        outData = outData.replace(a, b, 1)
                        print(f"* Replaced chunk ({a.count('\n')+1} -> {b.count('\n')+1} lines)")
                    return ''
                remain = re.sub(r"^<<<<<<+\s*(?:SEARCH)?\s*\n([\s\S]*?\n)======+\s*\n([\s\S]*?\n)>>>>>>+\s*(?:REPLACE)?\s*$", repl, delta, flags=re.MULTILINE)
                if err:
                    continue
                if remain.strip():
                    print(f"* ERROR: Some parts of the patch could not be applied:\n\n{delta}\n")
                    continue
                break # success!
            else:
                print("* ERROR: Failed to apply patch after 3 attempts")
                exit(1)

            if outData:
                outData = outData.rstrip("\n") + "\n" # Always end in exactly one newline

                if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", file]).returncode:
                    print(f"* Creating backup {file + ".bak"}")
                    with open(file + ".bak", "w") as f:
                        f.write(inData)
                else:
                    print(f"* Skipping backup (file committed in git)")

                with open(file, "w") as f:
                    f.write(outData)

            print()

        exit()
    else:
        print(f"Querying {model}...", end="", flush=True)
        text = run_completion(messages)

# Highlight any code block and copy it to the clipboard
def found_code_block(m):
    try:
        pyperclip.copy(m.group(2))
    except pyperclip.PyperclipException:
        pass
    return f"```{m.group(1)}\n\033[92m{m.group(2)}\033[0m\n```"
text = re.sub(r"^```(.*)\n([\s\S]*?)\n```", found_code_block, text, flags=re.MULTILINE)

# Show result
print("\r\033[K" + text)

