# Studio automation

Select a project, then open Scheduled tasks from the sidebar. The Tasks tab opens
by default. Create a task with a
name, prompt, model, and schedule. A task stays in the project where it was created;
switching projects shows that project's tasks and history. Choose once, daily,
weekdays, weekly, monthly, or a fixed interval. All
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
the current conversation model, and only explicitly selected files.
Later conversation changes do not alter it. Each execution has an independent
conversation context and shares the workspace and long-term memory with ordinary
conversations in the same project. The response reports its name, schedule,
and next run time.

Conversations also support queries, edits, pause, enable, run-now, and deletion.
Explicit requests act directly, including deletion; ambiguous references are clarified.
You can also ask whether a task ran, read its results, or request files it already
produced. Retrieving existing files does not rerun the task. Reference files and
execution outputs are shown separately. Tasks can also be managed in Plan mode.
Automation tasks and subagents cannot manage other tasks.

Automation tasks always use Full access. Both the main agent and subagents can run
commands and import, generate, edit, delete, or deliver files without individual
approvals, including image generation and browser screenshots. Ordinary conversations
can still use Ask before writing. Automation results are read-only: a
human-interaction interrupt fails that execution. Studio never automatically approves
or resumes it.

Task cards show an instruction preview, schedule, and next run. Search, pause,
enable, or run a task manually. The run button appears when a card is hovered or
has keyboard focus; it stays visible on touch devices. Select the card to edit the
full instructions.
Deletion from the task page requires confirmation, cancels queued work, and preserves history. It does not forcibly stop work that has already started.
Pausing affects future scheduled triggers.

Filter execution history by date, name, and status, then open a result to read messages
or download files. The calendar groups runs by queue time. Deleting a task retains historical files.

Each started execution appears as a separate conversation in its project after it
ends. The model generates a conversation title from the task prompt; you can also
rename it manually. Run-now
stays on the current page and shows submission, queue, and execution progress on the card.
While Studio is connected, completions and failures from all your projects produce
a notification. Initial load
and reconnection refresh status without replaying offline completion notifications.
Select **View linked conversation** in the result panel for follow-up questions; its task result
continues to show only the original execution. Moving a conversation changes where
it opens, while task history remains in the task's project. Deleting an automation
conversation removes its entry and retains the execution history, messages, and files.

[Studio quick start](quick_start.md) · [Documentation](../index.md)
