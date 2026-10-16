"""框架异常分类到 Studio 业务错误的完整映射契约"""

from tinkerfin_messaging import MessagingError, MessagingErrorCode
from tinkerfin_studio.api.errors import (
    BusinessException,
    ConversationErrorCode,
    SystemException,
)
from tinkerfin_studio.conversation.service import (
    _MESSAGING_ERRORS,
    ConversationChatService,
)


def _error(code: MessagingErrorCode) -> MessagingError:
    error_type = type(
        f"Test{code.name.title()}Error",
        (MessagingError,),
        {"code": code},
    )
    return error_type("safe framework message")


def test_messaging_error_mapping_covers_every_framework_code() -> None:
    assert set(_MESSAGING_ERRORS) == set(MessagingErrorCode)

    for code, (expected, business) in _MESSAGING_ERRORS.items():
        mapped = ConversationChatService._messaging_error(_error(code))
        assert mapped.error_code is expected
        assert isinstance(
            mapped,
            BusinessException if business else SystemException,
        )


def test_cancel_maps_producer_failure_to_cancel_specific_code() -> None:
    mapped = ConversationChatService._messaging_error(
        _error(MessagingErrorCode.RUN_PRODUCER_FAILED),
        operation="cancel",
    )

    assert isinstance(mapped, SystemException)
    assert mapped.error_code is ConversationErrorCode.RUN_CANCEL_FAILED
