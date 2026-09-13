"""会话错误日志的身份关联、原始异常链与凭据保护"""

import logging

from pydantic import SecretStr

from tinkerfin import RunIdentity
from tinkerfin_studio.conversation.error_logging import log_conversation_error
from tinkerfin_studio.models.schemas import AgentModelConfig


async def test_failure_log_keeps_cause_and_stack_without_model_keys(caplog):
    chat_key, image_key = "chat-private-credential", "image-private-credential"
    model = AgentModelConfig(
        model_id="configured-model",
        display_name="模型",
        provider="openai",
        model_name="upstream-model",
        base_url="https://example.invalid/v1",
        api_key=SecretStr(chat_key),
        reasoning_enabled=False,
    )
    image_model = model.model_copy(update={"api_key": SecretStr(image_key)})
    try:
        try:
            raise ValueError(f"provider rejected {chat_key} {image_key}")
        except ValueError as cause:
            raise RuntimeError("initialization failed") from cause
    except RuntimeError as error:
        with caplog.at_level(logging.ERROR):
            await log_conversation_error(
                identity=RunIdentity(
                    namespace="ns_1", thread_id="thread-a", run_id="run-a"
                ),
                model=model,
                image_model=image_model,
                code="runtime_initialization_error",
                error=error,
            )
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.ERROR
    for expected in [
        "thread-a",
        "run-a",
        "configured-model",
        "upstream-model",
        "ValueError",
        "RuntimeError",
        "test_failure_log_keeps_cause_and_stack_without_model_keys",
    ]:
        assert expected in caplog.text
    assert chat_key not in caplog.text
    assert image_key not in caplog.text
    assert "[redacted]" in caplog.text
