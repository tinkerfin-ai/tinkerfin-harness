"""Studio 的 Plan 澄清表单契约"""

from __future__ import annotations

from typing import ClassVar, Self

from pydantic import Field, model_validator

from tinkerfin.plan import (
    BuiltInClarificationForm,
    ClarificationModel,
    ClarificationOptionBase,
    SingleChoiceQuestion,
)


class StudioPlanOptionAttributes(ClarificationModel):
    """Studio 澄清选项的展示属性"""

    recommended: bool = Field(description="是否为 Planner 推荐的选项")


class StudioPlanClarificationOption(ClarificationOptionBase):
    """带有 Studio 展示属性的澄清选项"""

    attributes: StudioPlanOptionAttributes = Field(
        description="Studio 用于渲染选项的展示属性"
    )


class StudioPlanClarificationForm(
    BuiltInClarificationForm[ClarificationModel, StudioPlanClarificationOption]
):
    """按上海时区展示时间的澄清表单"""

    default_time_zone: ClassVar[str] = "Asia/Shanghai"

    title: str = Field(
        description="根据本次澄清问题生成简洁、用户可见的表单标题",
        min_length=1,
        max_length=20,
    )
    description: str = Field(
        description="说明回答这些问题将如何影响本次计划",
        min_length=1,
        max_length=60,
    )

    @model_validator(mode="after")
    def single_choice_recommendation_is_unique(self) -> Self:
        """确保单选题把唯一推荐项放在第一位"""

        for question in self.questions:
            if not isinstance(question, SingleChoiceQuestion):
                continue
            if not question.options[0].attributes.recommended:
                raise ValueError("单选题的第一个选项必须是推荐项")
            if any(option.attributes.recommended for option in question.options[1:]):
                raise ValueError("单选题只能标记第一个选项为推荐项")
        return self


__all__ = ["StudioPlanClarificationForm"]
