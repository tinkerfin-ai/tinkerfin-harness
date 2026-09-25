# Studio automation

Open Automation from the sidebar and create a task with a name, prompt, model, and
schedule. Choose once, daily, weekdays, weekly, monthly, or a fixed interval. All
times use Beijing time. Weekdays mean Monday through Friday without public-holiday
adjustments; nonexistent monthly dates are skipped. Start and end dates include
the whole day. The active period limits scheduled runs; manual runs remain available.
Without a start date, interval tasks first run after one interval. With a start date,
the interval starts at midnight on that date.

Tasks use the selected model's configuration and can reference up to five uploaded files.
Select another model if the chosen one is unavailable.

You can also ask in a conversation, for example, “Summarize industry news every day
at 09:00 Beijing time.” A complete request creates and enables the task directly;
missing execution details are clarified first. The task saves independent instructions,
the current conversation model and access mode, and only explicitly selected files.
Later conversation changes do not alter it. Each execution has an independent
conversation context and shares your workspace and long-term memory with ordinary
conversations. The response reports its name, schedule,
and next run time.

Conversations also support queries, edits, pause, enable, run-now, and deletion.
Explicit requests act directly, including deletion; ambiguous references are clarified.
You can also ask whether a task ran, read its results, or request files it already
produced. Retrieving existing files does not rerun the task. Reference files and
execution outputs are shown separately. Plan mode also has these task-management
tools. Background tasks and subagents receive no task-management tools.

Conversations and tasks default to Full access (`full`). Require write approval
(`write_approval`) requires approval for command execution and for importing, generating,
editing, deleting, or delivering files, including image generation and browser screenshots.
The same policy applies in Plan mode and to subagents; approving a tool does not approve
a Plan draft. This is not a read-only sandbox policy. Permissions cannot change during
a conversation run; resuming or branching retains the source run's permission.
Automation results are read-only: an approval or other human-interaction interrupt
fails that execution. Studio never automatically approves or resumes it.

The task list shows the next run. Search, pause, enable, or run a task manually.
Deletion from the task page requires confirmation, cancels queued work, and preserves history. It does not forcibly stop work that has already started.
Pausing affects future scheduled triggers.

Filter execution history by date, name, and status, then open a result to read messages
or download files. The calendar groups runs by queue time. Deleting a task retains historical files.

[Studio quick start](quick_start.md) · [Documentation](../index.md)
