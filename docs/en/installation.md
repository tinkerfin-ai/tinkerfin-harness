# Installation and optional components

[Documentation](index.md) · [中文](../cn/installation.md)


Python 3.11 or newer is required.

| Capability | Command |
| --- | --- |
| Native Runtime, Plan Mode, and Observation | `pip install tinkerfin` |
| OpenAI model adapter used by examples | `pip install langchain-openai` |
| AG-UI Runtime | `pip install "tinkerfin[agui]"` |
| AG-UI conversion only | `pip install tinkerfin-agui-adapter` |
| Shared run and observation contracts | `pip install tinkerfin-contracts` |
| Shared Native stream contract | `pip install tinkerfin-native-stream` |
| In-memory semantic tracing | `pip install tinkerfin-tracing` |
| SQL Trace persistence | `pip install "tinkerfin-tracing[sqlalchemy]"` + your async database driver |
| LangGraph memory with SQLAlchemy | `pip install "tinkerfin-langgraph-store[sqlalchemy]"` plus an async database driver |
| Process-local resource notifications | `pip install tinkerfin-notifications` |
| Cross-process resource notifications | `pip install "tinkerfin-notifications[redis]"` |
| Runtime commands and durable output | `pip install tinkerfin-gateway` |
| Gateway HTTP responses | `pip install "tinkerfin-gateway[starlette]"` |
| Protocol-neutral in-memory messaging | `pip install tinkerfin-messaging` |
| AG-UI messaging codec | `pip install "tinkerfin-messaging[agui]"` |
| Native messaging codec | `pip install "tinkerfin-messaging[native]"` |
| SQL message storage | `pip install "tinkerfin-messaging[sqlalchemy]"` plus an async database driver |
| Redis AG-UI message storage | `pip install "tinkerfin-messaging[agui,redis]"` |
| OpenSandbox | `pip install tinkerfin-sandbox` |
| SQL Sandbox state | `pip install "tinkerfin-sandbox[sqlalchemy]"` plus an async database driver |
| Immediate execution and task scheduling with in-memory defaults | `pip install tinkerfin-automation` |
| SQL task persistence | `pip install "tinkerfin-automation[sqlalchemy]"`; install an async driver separately |

You do not need every package at once. Start with the capability you need now.
