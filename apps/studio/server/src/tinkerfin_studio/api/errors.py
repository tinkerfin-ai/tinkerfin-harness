"""业务错误码、异常和 HTTP 投影"""

from __future__ import annotations

import logging
from enum import IntEnum

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from tinkerfin_studio.api.responses import ApiResponse

logger = logging.getLogger(__name__)


class _ErrorCodeValue(int):
    """在创建整数枚举成员前携带响应元数据"""

    http_status: int
    message: str

    def __new__(
        cls,
        code: int,
        http_status: int,
        message: str,
    ) -> _ErrorCodeValue:
        value = int.__new__(cls, code)
        value.http_status = http_status
        value.message = message
        return value


class ErrorCode(IntEnum):
    """同时携带 HTTP 状态与安全消息的业务错误码"""

    http_status: int
    message: str

    def __new__(cls, value: _ErrorCodeValue) -> ErrorCode:
        member = int.__new__(cls, int(value))
        member._value_ = int(value)
        member.http_status = value.http_status
        member.message = value.message
        return member


class GlobalErrorCode(ErrorCode):
    """跨模块通用错误"""

    BAD_REQUEST = _ErrorCodeValue(400, 400, "请求参数不正确")
    UNAUTHORIZED = _ErrorCodeValue(401, 401, "请先登录")
    FORBIDDEN = _ErrorCodeValue(403, 403, "没有该操作权限")
    NOT_FOUND = _ErrorCodeValue(404, 404, "请求未找到")
    METHOD_NOT_ALLOWED = _ErrorCodeValue(405, 405, "请求方法不正确")
    CONFLICT = _ErrorCodeValue(409, 409, "请求状态冲突")
    VALIDATION_FAILED = _ErrorCodeValue(422, 422, "请求参数校验失败")
    INTERNAL_SERVER_ERROR = _ErrorCodeValue(500, 500, "系统异常")
    SERVICE_UNAVAILABLE = _ErrorCodeValue(503, 503, "服务暂不可用")


class AutomationErrorCode(ErrorCode):
    """自动化任务和运行接口的可恢复错误"""

    NOT_FOUND = _ErrorCodeValue(1_001_007_000, 404, "任务或运行记录不存在")
    CONFLICT = _ErrorCodeValue(1_001_007_001, 409, "任务已变化，请重新加载后再操作")
    INVALID_CONFIGURATION = _ErrorCodeValue(
        1_001_007_002, 422, "任务配置、日程或分页条件不正确"
    )
    QUEUE_FULL = _ErrorCodeValue(1_001_007_003, 409, "任务队列已满，请等待已有任务结束")
    UNAVAILABLE = _ErrorCodeValue(1_001_007_004, 503, "自动化服务暂不可用，请稍后重试")


class AuthErrorCode(ErrorCode):
    """认证模块错误"""

    BAD_CREDENTIALS = _ErrorCodeValue(1_001_001_000, 401, "用户名或密码错误")
    USER_DISABLED = _ErrorCodeValue(1_001_001_001, 403, "用户已被禁用")
    SERVICE_UNAVAILABLE = _ErrorCodeValue(1_001_001_002, 503, "认证服务暂不可用")


class ModelErrorCode(ErrorCode):
    """模型目录错误"""

    NOT_FOUND = _ErrorCodeValue(1_001_005_000, 422, "模型不存在")
    DISABLED = _ErrorCodeValue(1_001_005_001, 409, "模型已停用")
    CATALOG_UNAVAILABLE = _ErrorCodeValue(1_001_005_002, 503, "模型目录暂不可用")
    INVALID_CONFIGURATION = _ErrorCodeValue(1_001_005_003, 422, "模型配置不正确")
    IN_USE = _ErrorCodeValue(
        1_001_005_004, 409, "该模型仍有运行或审批未结束，请结束后再修改或删除"
    )
    KEY_REQUIRED = _ErrorCodeValue(1_001_005_005, 422, "新增模型需要填写 API 密钥")
    KEY_ENDPOINT_CHANGED = _ErrorCodeValue(
        1_001_005_006, 422, "更换服务地址时需要重新填写对应密钥"
    )
    PURPOSE_MISMATCH = _ErrorCodeValue(
        1_001_005_007, 409, "模型用途不匹配，请选择对应用途的模型"
    )
    CONFIGURATION_CHANGED = _ErrorCodeValue(
        1_001_005_008, 409, "模型配置已变化，请重新发送"
    )


class AttachmentErrorCode(ErrorCode):
    REFERENCE_CONFLICT = _ErrorCodeValue(1_001_006_020, 409, "文件引用与原请求不一致")
    """附件校验、权限和交付错误"""

    INVALID_FILE = _ErrorCodeValue(
        1_001_006_000, 422, "文件内容或名称不符合要求，请检查后重新上传"
    )
    TOO_LARGE = _ErrorCodeValue(
        1_001_006_001, 413, "单个附件不能超过 10 MiB，一次附件总大小不能超过 25 MiB"
    )
    NOT_FOUND = _ErrorCodeValue(
        1_001_006_002, 404, "附件不可用或不属于当前会话，请重新上传"
    )
    THREAD_UNAVAILABLE = _ErrorCodeValue(1_001_006_003, 404, "附件所属会话不可用")
    INVALID_VARIANT = _ErrorCodeValue(1_001_006_004, 422, "当前附件不支持所选预览方式")
    ALREADY_SENT = _ErrorCodeValue(1_001_006_005, 409, "已发送的附件随会话保留")
    UPLOAD_IN_PROGRESS = _ErrorCodeValue(1_001_006_007, 409, "附件正在处理，请稍后再试")
    UPLOAD_TIMEOUT = _ErrorCodeValue(1_001_006_006, 408, "附件上传超时，请重试")


class ConversationErrorCode(ErrorCode):
    """会话与分布式运行错误"""

    NOT_FOUND = _ErrorCodeValue(1_001_004_000, 404, "会话不存在")
    INVALID_CURSOR = _ErrorCodeValue(1_001_004_001, 422, "无效的分页游标")
    RUN_CONFLICT = _ErrorCodeValue(
        1_001_004_002,
        409,
        "会话当前状态不允许启动新的运行",
    )
    DELETE_CONFLICT = _ErrorCodeValue(
        1_001_004_003,
        409,
        "会话仍在运行，请先停止并等待运行结束",
    )
    MESSAGING_UNAVAILABLE = _ErrorCodeValue(
        1_001_004_004,
        503,
        "会话消息服务暂不可用",
    )
    USER_MESSAGE_REQUIRED = _ErrorCodeValue(
        1_001_004_006,
        422,
        "初次运行必须包含文本 user 消息",
    )
    RESUME_REQUIRED = _ErrorCodeValue(1_001_004_013, 422, "resume 不能为空")
    RESUME_THREAD_ID_REQUIRED = _ErrorCodeValue(
        1_001_004_017,
        422,
        "恢复运行时 threadId 不能为空",
    )
    INVALID_LAST_EVENT_ID = _ErrorCodeValue(
        1_001_004_019,
        400,
        "Last-Event-ID 必须是规范非负整数",
    )
    RUN_NOT_FOUND = _ErrorCodeValue(1_001_004_020, 404, "会话运行不存在")
    RUN_IDENTITY_CONFLICT = _ErrorCodeValue(
        1_001_004_021,
        409,
        "相同 runId 的请求内容不一致",
    )
    RUN_CANCEL_UNSUPPORTED = _ErrorCodeValue(
        1_001_004_022,
        409,
        "当前运行不支持取消",
    )
    TRACE_UNAVAILABLE = _ErrorCodeValue(
        1_001_004_023,
        503,
        "会话 Trace 暂不可用",
    )
    RUN_CANCEL_FAILED = _ErrorCodeValue(1_001_004_025, 500, "取消会话运行失败")
    RESUME_ALREADY_CLAIMED = _ErrorCodeValue(
        1_001_004_026,
        409,
        "该审批已被另一次恢复运行认领",
    )
    MESSAGING_FAILURE = _ErrorCodeValue(1_001_004_027, 500, "会话消息处理失败")
    MESSAGING_QUOTA_EXCEEDED = _ErrorCodeValue(
        1_001_004_029,
        413,
        "会话事件超过持久化容量限制",
    )
    PENDING_INTERRUPT = _ErrorCodeValue(1_001_004_030, 409, "请先处理当前待审批项")
    REQUEST_TOO_LARGE = _ErrorCodeValue(
        1_001_004_031,
        413,
        "用户消息超过当前容量限制",
    )
    MESSAGING_STREAM_EXPIRED = _ErrorCodeValue(
        1_001_004_032,
        410,
        "实时重播窗口已过期，请重新加载会话",
    )


class ApplicationException(Exception):
    """携带稳定错误码的应用异常"""

    def __init__(self, error_code: ErrorCode, *, message: str | None = None) -> None:
        self.error_code = error_code
        self.message = message or error_code.message
        super().__init__(self.message)


class BusinessException(ApplicationException):
    """调用方可以理解并修正的业务失败"""


class SystemException(ApplicationException):
    """仅向调用方暴露安全消息的技术失败"""


def _response(error_code: ErrorCode, *, message: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=error_code.http_status,
        content=ApiResponse[None](
            code=int(error_code),
            message=message or error_code.message,
        ).model_dump(mode="json"),
    )


async def application_exception_handler(
    request: Request,
    error: ApplicationException,
) -> JSONResponse:
    """记录应用异常并返回稳定包络"""

    log = logger.warning if isinstance(error, BusinessException) else logger.error
    log(
        "应用异常: method=%s path=%s code=%s",
        request.method,
        request.url.path,
        int(error.error_code),
        exc_info=not isinstance(error, BusinessException),
    )
    return _response(error.error_code, message=error.message)


async def request_validation_handler(
    request: Request,
    _error: RequestValidationError,
) -> JSONResponse:
    """把请求校验失败投影为统一错误"""

    logger.warning("请求校验失败: method=%s path=%s", request.method, request.url.path)
    return _response(GlobalErrorCode.VALIDATION_FAILED)


async def http_exception_handler(
    request: Request,
    error: HTTPException,
) -> JSONResponse:
    """把 Starlette HTTP 异常投影为统一错误"""

    codes = {
        400: GlobalErrorCode.BAD_REQUEST,
        401: GlobalErrorCode.UNAUTHORIZED,
        403: GlobalErrorCode.FORBIDDEN,
        404: GlobalErrorCode.NOT_FOUND,
        405: GlobalErrorCode.METHOD_NOT_ALLOWED,
        409: GlobalErrorCode.CONFLICT,
        422: GlobalErrorCode.VALIDATION_FAILED,
        503: GlobalErrorCode.SERVICE_UNAVAILABLE,
    }
    logger.warning(
        "HTTP 异常: method=%s path=%s status=%s",
        request.method,
        request.url.path,
        error.status_code,
    )
    return _response(
        codes.get(error.status_code, GlobalErrorCode.INTERNAL_SERVER_ERROR)
    )


async def unexpected_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """记录未识别异常并隐藏内部细节"""

    logger.error(
        "未处理异常: method=%s path=%s",
        request.method,
        request.url.path,
        exc_info=error,
    )
    return _response(GlobalErrorCode.INTERNAL_SERVER_ERROR)
