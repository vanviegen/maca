I want you to design a agentic coding assistant. It should have the following properties:

- The app should employ multiple different contexts, possibly with different models, and with different system prompts.
  - Main Context: This is the primary context where the user interacts with the assistant for general coding tasks. It should use a powerful model like Claude Sonnet. It's responsible for spawning other contexts and verifying their output. It is important that this context always stays small. Therefore, it should never need to read large code bases. It asks other contexts to do that, and allocates a number of characters that these contexts may return as a response. (If the response is too large, the other context is asked to summarize it further.)
  - Code Analysis Context: This context is responsible for reading and analyzing large code bases, and create/updating an AI-ARCHITECTURE.md file. 
  - Research Context: This context is responsible for looking up information on the web, and summarizing relevant findings.
  - Implementation Context: This context is responsible for writing code, given specifications from the Main Context. For simple tasks, a fast/cheap model may be selected for this by the main context. If the results are not satisfactory, the main context may re-allocate the task to a more powerful model.
  - Review Context: This context is responsible for reviewing code written by the Implementation Context, ensuring it meets quality standards and adheres to best practices, and actually solves the problem at hand.
  - Merge Context: Resolves merge conflicts.
- Each context should have its own system prompt, tailored to its specific role.
- After each subcontext LLM (intermediate) response (which can just be a tool call), the Main Context will do a quick check to see if the subcontext is still one the right track. If not, it will provide corrective feedback (or decide to terminate the subcontext and spawn a new one).
- Every session must create a structure log file in `.aai/<session_id>.log`. It just takes the first available integer as its session id. It should contain lines of JSON, and containing the complete history, so it may be resumed from disk as a later time.
- When the app is started, it first verifies that the current directory is a git repository. If not, it asks to initializes a new git repository. Without git repo, it refuses to proceed. Then, it first creates a new branch (named with just the session_id), and then creates a new worktree checkout in `.trees/<session_id>`. This is where all work will be done. This way, multiple tasks can be worked on in parallel.
- After every tool call, a git commit is done (with the LLM response brief_summary as commit message). (If the tool call made no changes, no commit is done.)
- Once the main context thinks the task is done, it will prompt the user if it may merge into main. If yes, it will squash all commits into one (with a proper commit messages that does *not* describe any intermediate steps taken, but just the end result). Then a rebase happens, and then a ff merge into main, optionally asking a new subcontext to resolve conflicts if any arise. If succeeded, the worktree and the branch are reset to main branch, ready for a new task. (Though the main context is preserved, in case the user wants to continue working on a related task.)
- Subcontexts must always respond with a single tool call (for broad compatibility through openrouter). Each tool is a Python function defined in the app. Tool arguments and docs should be derived from just this function by reflection. Each tool call gets an automatic argument `rationale` that explains why the tool is being called. Here are the most important tools:
  - read_files({file_path: str, start_line: int = 1, max_lines: int = 100}) -> [{"file_path": str, "data": str, "remaining_lines": int}[]]: (Partially) read one or more files.
  - list_files(glob_pattern: str = "**", max_files: int = 200) -> [str]: Lists up to max_files file paths in the worktree matching the specified glob pattern.
  - update_files(({file_path: str, data: str} | {file_path: str, old_data: str, new_data: str, allow_multiple=false})[]) -> void: Writes to or updates multiple files in one call. Each item in the list can either write the entire file (recommended for large updates) or search/replace content in the file (for local changes).
  - search(glob_pattern: str, regex: str, max_results: int = 10, lines_before: int = 2, lines_after: int = 2) -> [{"file_path": str, "line_number": int, "lines": str}]: Searches for files in the worktree matching the glob pattern and containing lines that match the regex. Returns up to max_results file paths. 
  - shell(command: str, docker_image: str = "debian:stable", docker_runs: str[] = [], head: number = 50, tail: number = 50) -> {"stdout": str, "stderr": str, "exit_code": int}: Executes the specified shell command in a docker container, returning the output. A Dockerfile is created on the fly to perform any RUN commands if specified. The AI is encouraged to use the same docker image and the same RUN commands (or otherwise appending RUN commands if needed), for faster builds. Both stdout and stderr are captured and combined. If the output is longer than tail+head lines, the middle part of removed (indicated by "\n\n... 1234 more lines stripped (changed head/tail to see them, or use a grep to search for specific output) ...\n\n"). The docker environment is ephemeral and discarded after each command. It should actually use podman if available, for rootless execution, so file permissions are preserved. It should create a volume mount for the worktree and for the .git directory (both mapped at the same path inside the container as in the host).
  - ask_question(question): str: Adds the question to the main context (adding the hint that it may want to relay the question to the user). The main context is expected to provide the answer using the continue_subcontext guidance string. (Or it may decide not to continue with this subcontext if it's going in the wrong direction.)
  - complete(result: str) -> void: Indicates that the task is complete/cannot be completed, with the provided result summary for the main context. 
- The Main Context must also always do a function call. It can be one of:
  - get_user_input(prompt: str, preset_answers: str[] = []) -> {"user_input": str}: Prompts the user for input with the specified prompt. Optionally allowing the user to select from preset answers (but a custom answer is always allowed).
  - create_subcontext(context_type: str, task_description: str, model: str = "claude-2", max_response_chars: int = 2000) -> {"subcontext_id": str}: Spawns a new subcontext of the specified type, with the given task description. The model and max_response_chars can be adjusted as needed.
- The AGENTS.md should be maintained. It should always include the default docker_image and docker_runs to be used for most shell commands. 
- The app is written in Python.
- Prompts are stored in separate Tenjin files (https://pypi.org/project/Tenjin/) for easy editing.

- To clarify the flow:
  1. User starts the app in the project dir
  2. App verifies git repo, or asks to initializes a new one.
  3. A new branch and worktree is created for the session.
  4. User is asked for the main prompt (or it can be provided as a command line argument), which is added to the main context.
  5. The Main Context runs its LLM completion
  6. Main Context returns with a tool call:
     - get_user_input: After the user is consulted, the response is added to the context and we go back to [5]
     - create_subcontext(unique_name: str): A new subcontext is created, and its task description is added to its context. We go to [7]
     - continue_subcontext(unique_name: str, guidance: string?): The subcontext is continued, optionally providing it some guidance prompt. we go to [7]
     - complete: The task is done, we ask the user. if not okay, we add user feedback to the context and go back to [5]. If okay, we'll proceed to merge into main. (Possibly spawning a Merge Context if needed.) Then we'll reset the worktree and branch to main, and go back to [4] for a new task.    
  7. The subcontext runs its LLM completion
  8. The subcontext returns with a tool call:
        - The tool is executed, the response is added to the subcontext
        - A `git diff --numstat` is done and the result is stored in a variable
        - If the diff is not empty, a git commit is made (with the rationele as message)
        - A summary of what happened (LLM tokens used in the last run, the tool called, tool call duration, its rationale, and the result of the `git diff --numstat`) is added to the *main* context. Together with a prompt on whether it wants to continue running the subcontext (using 'continue_subcontext') or do something else
   9. We go back to 1. (The main context decides what to do next, which could be continue_subcontext. The system prompt should strongly encourage the main context to allow subcontexts to continue working until they indicate completion, rather than terminating them early, unless it's clearly going of track)
           - We go back to [1]
