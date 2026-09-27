# Glossary

[Documentation](index.md) · [中文](../cn/glossary.md)


| Term | Plain meaning |
| --- | --- |
| Agent | A program that uses a model and tools to complete a task |
| Graph | The workflow that actually executes the agent |
| Runtime | A reusable object that executes a configured agent through calls or streams |
| Plan Mode | A workflow for clarifying and approving a plan before execution |
| stream | Data produced continuously while a run is active |
| namespace | Application-selected business isolation scope |
| RunIdentity | Framework identity containing `namespace`, `thread_id`, and `run_id` |
| thread | A continuing conversation identified inside one namespace |
| run | One semantic execution identified inside a thread |
| graph namespace | Position inside an execution Graph; separate from business namespace |
| Trace | Recorded history of conversations, model calls, tools, and subagents |
| AG-UI | A protocol for exchanging live agent events with a frontend |
| SSE | An HTTP format for sending a continuing event stream to a browser |
| Messaging | The component that persists and delivers streams |
| Sandbox | An isolated environment where an agent can use files and commands |
| Notifications | Advisory resource-change broadcast for independent listeners |
| Gateway | Combines authorized run commands, durable output, and browser notifications |
