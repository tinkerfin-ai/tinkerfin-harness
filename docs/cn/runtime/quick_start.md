# 运行第一个智能体

[快速开始](../quick_start.md) · [English](../../en/runtime/quick_start.md)

需要 Python 3.11 或更高版本。以下示例使用 OpenAI 的 LangChain 提供方：

```bash
python -m venv .venv
source .venv/bin/activate
pip install tinkerfin langchain-openai
```

设置 `OPENAI_API_KEY` 后运行：

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
                {"role": "user", "content": "请用一句话问好。"}
            ]
        },
    )
    print(result["messages"][-1].content)


asyncio.run(main())
```

持续对话复用同一个 `thread_id`，每次新输入使用新的 `run_id`。只有配置 checkpointer 后，thread 才会持久保存。

下一步：[配置智能体与 Plan](agent-configuration.md)。
