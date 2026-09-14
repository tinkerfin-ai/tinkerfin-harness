# Studio automation

Open Automation from the sidebar and create a task with a name, prompt, model, and
schedule. Choose once, daily, weekdays, weekly, monthly, or a fixed interval. All
times use Beijing time. Weekdays mean Monday through Friday without public-holiday
adjustments; nonexistent monthly dates are skipped. Start and end dates include
the whole day. The active period limits scheduled runs; manual runs remain available.

Tasks use the selected model's configuration and can reference up to five uploaded files.
Select another model if the chosen one is unavailable.

Conversations and tasks default to Full access (`full`). Require write approval
(`write_approval`) enables `write_file` approval for the main agent and subagents;
it is not a read-only sandbox policy. Permissions cannot change during a conversation
run; resuming or branching retains the source run's permission.
Automation results are read-only: an approval or other human-interaction interrupt
fails that execution. Studio never automatically approves or resumes it.

The task list shows the next run. Search, pause, enable, or run a task manually.
Deletion requires confirmation, cancels queued work, and preserves history. It does not forcibly stop work that has already started.
Pausing affects future scheduled triggers.

Filter execution history by date, name, and status, then open a result to read messages
or download files. The calendar groups runs by queue time. Deleting a task retains historical files.

[Studio quick start](quick_start.md) · [Documentation](../index.md)
