<p align="right">
  <a href="README.md">简体中文</a>
</p>
<p align="center">
  <img src="apps/studio/web/public/brand/tinkerfin.svg" alt="TinkerFin" width="88" />
</p>
<h1 align="center">TinkerFin</h1>
<p align="center">A Python agent harness for enterprise applications and workflows.</p>
<p align="center">
  <a href="docs/en/runtime/quick_start.md"><img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&amp;logo=python&amp;logoColor=white" /></a>
  <a href="https://www.typescriptlang.org/"><img alt="TypeScript" src="https://img.shields.io/badge/TypeScript-0891B2?style=flat-square&amp;logo=typescript&amp;logoColor=white" /></a>
  <a href="https://docs.langchain.com/oss/python/deepagents/overview"><img alt="Deep Agents" src="https://img.shields.io/badge/Deep_Agents-7C3AED?style=flat-square" /></a>
  <a href="https://docs.langchain.com/oss/python/langchain/overview"><img alt="LangChain" src="https://img.shields.io/badge/LangChain-D97706?style=flat-square&amp;logo=langchain&amp;logoColor=white" /></a>
  <a href="https://fastapi.tiangolo.com/"><img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&amp;logo=fastapi&amp;logoColor=white" /></a>
  <a href="https://github.com/opensandbox-group/OpenSandbox"><img alt="OpenSandbox" src="https://img.shields.io/badge/OpenSandbox-E11D48?style=flat-square" /></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-52617A?style=flat-square" /></a>
  <a href="docs/en/index.md"><img alt="Docs: English / 中文" src="https://img.shields.io/badge/Docs-English%20%2F%20中文-2563EB?style=flat-square" /></a>
</p>

## What it is

**TinkerFin is a Python agent harness for enterprise applications.** It connects
models, tools, and workspaces, with multimodal input, task orchestration, persistent
state, human approval, and execution tracing.

Run agents independently or combine them with business workflows for research,
document processing, data analysis, and file generation.

## Personal spaces, projects, and live updates

Each Studio account manages its own projects, model and service connections, and personal skills library.

- **Organize work by project**: Projects keep conversations, workspace files, long-term memory, and scheduled tasks separate. Conversations in the same project share files and memory. Personal skills work across your projects, with project-specific skills available when needed.
- **Continue across clients**: Sign in to the same account from different browsers or devices to follow conversations and run status. Project resources and file changes notify other clients to refresh; reconnecting restores the latest state.
- **Use directories and tools on a Mac**: Click **连接** (Connect) in Companion and complete authorization in your browser, then select a project and switch to local execution. The client prepares its workspace automatically; follow approvals and results in the conversation. See [local collaboration](docs/en/studio/local_devices.md).
- **Drafts and file previews**: Unsent drafts stay in the current page. When an open workspace text file changes, Studio offers a preview refresh so the content you are reading is not replaced unexpectedly.

![Project conversations, file delivery, and live updates across clients](docs/assets/screenshots/studio-en.gif)

## Installation

Install the Python framework and the model integration you plan to use:

```bash
pip install tinkerfin langchain-openai
```

For Studio, use Docker Compose for dependencies and Python with uv for the local backend. The [Studio setup guide](docs/en/studio/quick_start.md)
covers the required services and configuration.

## Quick Start

### Try Studio

Run `./apps/studio/server/deploy/start.sh --local` to prepare dependencies and local configuration, then start the backend and Web client. Container deployment is also supported. Sign in with the initial username `tinkerfin` and password `123456`, then configure your model.

[Open the Studio setup guide →](docs/en/studio/quick_start.md)

### Build with Python

Requires Python 3.11 or newer. Set your model credentials, then build an `AgentRuntime` with
`TinkerFin().with_namespace(...).build(...)`, and consume its output.

[Run your first agent →](docs/en/runtime/quick_start.md)

## Core concepts

- **Compose your agent harness** — Configure models, tools, skills, and subagents. Add execution management, messaging, notifications, tracing, and sandbox capabilities as needed.
- **Connect agents and workflows** — Invoke agents from application workflows, or register compiled LangGraph workflows as subagents to combine predefined steps with model-directed tool use.
- **Multimodal input and file delivery** — Pass images, audio, video, and documents to models and tools that support their formats, and return generated files.
- **Review plans and tool operations** — Clarify requirements and approve a plan before execution. Selected tool operations can require separate approval.
- **Stream and replay events** — Deliver AG-UI events through persistent message channels and replay after reconnection. Application events can use the same channels.
- **Accept commands and refresh clients** — Use [Gateway](docs/en/gateway/index.md) for run commands and durable output, and [Notifications](docs/en/notifications/index.md) to refresh resources across connected clients.
- **Trace each call** — Inspect model, tool, and subagent relationships, inputs, outputs, and timing, live or from past runs.
- **Manage isolated workspaces** — Read and write files, execute commands, and reuse, prewarm, pause, or resume environments.
- **Persist state and coordinate runs** — Retain conversation state and continue after approval. Coordinate run ownership, duplicate requests, and cancellation across processes.
- **Multitenant integration** — Scope conversations, storage, and sandboxes by tenant, user, or project. Applications own authentication and access authorization.
- **Schedule background tasks** — Run operations immediately or on one-time, fixed-rate, and Cron schedules. Query results, cancel executions, and request retries through [Automation](docs/en/automation/index.md).

## Studio: a multimodal agent workspace

Start tasks with text, images, and documents. Analyze content, generate images, and receive files in the same conversation. Image understanding and generation require the corresponding models to be configured.

### Turn business goals into execution plans

Turn a business goal into a reviewable plan, then confirm the scope and steps before execution.

![Business clarification and plan review](docs/assets/screenshots/plan-en.gif)

### Understand how each task runs

Follow a business conversation through model calls, tools, and subagents to see how the task ran and where time was spent.

![Store data review through model and research subagent calls](docs/assets/screenshots/trace-en.gif)

### Schedule work and review the results

Create one-time or recurring tasks, follow their execution, and open the resulting conversation to continue working.

![Scheduled tasks and their result conversations](docs/assets/screenshots/automation-en.gif)

*Recorded in Studio demo projects with sample data.*

## Project layout

| Part | Purpose |
| --- | --- |
| `packages/` | Enterprise agent harness packages for execution, persistence, messaging, human review, tracing, and workspaces |
| `apps/studio/` | Studio interface for conversations, plan review, files, and traces |
| `docs/` | Bilingual documentation for application development, deployment, operations, and advanced integration |

## Documentation

[English documentation →](docs/en/index.md)

From application development to deployment and operations: getting-started guides, architecture documentation, and integration references.

## Contributing

Issue reports, documentation improvements, and code contributions are welcome. See the [development guide](docs/en/development.md) for environment setup and validation commands.

- [Contributing](CONTRIBUTING.md)
- [Security reports](SECURITY.md)

## License

[Apache License 2.0](LICENSE) is the repository default. `tinkerfin-langgraph-store` uses its packaged
[MIT License](packages/tinkerfin-langgraph-store/LICENSE) and [NOTICE](packages/tinkerfin-langgraph-store/NOTICE).
