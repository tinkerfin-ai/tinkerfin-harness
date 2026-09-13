# Run your first agent

[Quick Start](../quick_start.md) · [中文](../../cn/runtime/quick_start.md)

Python 3.11 or newer is required. This example uses the OpenAI LangChain provider:

```bash
python -m venv .venv
source .venv/bin/activate
pip install tinkerfin langchain-openai
```

Set `OPENAI_API_KEY`, then run:

```python
import asyncio

from tinkerfin import TinkerFin


runtime = (
    TinkerFin()
    .with_namespace("example")
    .build(model="openai:gpt-5.4")
)


async def main() -> None:
    result = await runtime.ainvoke(
        thread_id="hello",
        run_id="hello-1",
        input={
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ]
        },
    )
    print(result["messages"][-1].content)


asyncio.run(main())
```

Use the same `thread_id` for a continuing conversation and a new `run_id` for each new
input. A thread becomes persistent only when the Runtime has a checkpointer.

Next: [Configure agents and Plan](agent-configuration.md).
