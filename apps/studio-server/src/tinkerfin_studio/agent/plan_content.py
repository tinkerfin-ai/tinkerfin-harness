"""Studio 计划草稿正文契约"""

from typing import Annotated

from pydantic import Field, StringConstraints

from tinkerfin.plan import MarkdownPlanContent


class StudioMarkdownPlanContent(MarkdownPlanContent):
    """为 Studio 审阅卡片补充动态说明，正文继续使用 Markdown"""

    description: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=80),
    ] = Field(description="概括计划目标与执行重点的用户可见说明")


__all__ = ["StudioMarkdownPlanContent"]
