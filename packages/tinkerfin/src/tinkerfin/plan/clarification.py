"""Public strongly typed contracts for Plan clarification forms and responses."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Annotated, ClassVar, Generic, Literal, TypeAlias, TypeVar, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from pydantic.alias_generators import to_camel

ClarificationId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]
ClarificationText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]
ClarificationTypeId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[a-z][a-z0-9._-]*(?::[a-z][a-z0-9._-]*)*$",
    ),
]


class ClarificationModel(BaseModel):
    """Base for public, immutable clarification boundary models."""

    model_config = ConfigDict(
        allow_inf_nan=False,
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class ClarificationOptionBase(ClarificationModel):
    """Core fields shared by every model-generated choice option."""

    id: ClarificationId = Field(description="Stable option ID within one question")
    label: ClarificationText = Field(description="Concise option label shown to users")
    description: ClarificationText | None = Field(
        default=None,
        description="Optional user-visible consequence or tradeoff",
    )


OptionAttributesT = TypeVar("OptionAttributesT", bound=ClarificationModel)
OptionT = TypeVar("OptionT", bound=ClarificationOptionBase)


class ClarificationOption(ClarificationOptionBase, Generic[OptionAttributesT]):
    """Choice option with one host-defined, strongly typed attributes model."""

    attributes: OptionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )


class ClarificationQuestionBase(ClarificationModel):
    """Framework-owned fields shared by every clarification question."""

    id: ClarificationId = Field(description="Stable question ID within one form")
    prompt: ClarificationText = Field(description="Question shown to the user")
    required: bool = Field(
        description=(
            "Whether planning must stop until the user answers this question; "
            "optional questions may be explicitly skipped"
        )
    )


QuestionAttributesT = TypeVar("QuestionAttributesT", bound=ClarificationModel)


class _ChoiceQuestion(
    ClarificationQuestionBase,
    Generic[QuestionAttributesT, OptionT],
):
    """Shared immutable fields for built-in choice questions."""

    options: tuple[OptionT, ...]
    allow_free_text: bool = Field(
        default=True,
        description="Whether one custom answer is accepted as an alternative choice",
    )
    attributes: QuestionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )

    @model_validator(mode="after")
    def option_ids_are_unique(
        self,
    ) -> _ChoiceQuestion[QuestionAttributesT, OptionT]:
        """Require stable, unambiguous option addressing within one question."""

        option_ids = tuple(option.id for option in self.options)
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("Clarification option IDs must be unique")
        return self


class SingleChoiceQuestion(
    _ChoiceQuestion[QuestionAttributesT, OptionT],
    Generic[QuestionAttributesT, OptionT],
):
    """Question answered by exactly one checkpoint option or one custom answer."""

    answer_type: Literal["single_choice"] = "single_choice"
    options: tuple[OptionT, ...] = Field(min_length=1)


class MultipleChoiceQuestion(
    _ChoiceQuestion[QuestionAttributesT, OptionT],
    Generic[QuestionAttributesT, OptionT],
):
    """Question answered by options plus an optional custom answer."""

    answer_type: Literal["multiple_choice"] = "multiple_choice"
    options: tuple[OptionT, ...] = Field(min_length=2)
    min_selections: int = Field(
        default=1,
        ge=1,
        strict=True,
        description="Minimum number of options plus a possible custom answer",
    )
    max_selections: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        description="Optional maximum number of options plus a custom answer",
    )

    @model_validator(mode="after")
    def selection_bounds_are_satisfiable(
        self,
    ) -> MultipleChoiceQuestion[QuestionAttributesT, OptionT]:
        """Require model-generated selection bounds that users can satisfy."""

        capacity = len(self.options) + int(self.allow_free_text)
        if self.min_selections > capacity:
            raise ValueError("min_selections exceeds the available answer capacity")
        if self.max_selections is not None:
            if self.max_selections < self.min_selections:
                raise ValueError(
                    "max_selections must be greater than or equal to min_selections"
                )
            if self.max_selections > capacity:
                raise ValueError("max_selections exceeds the available answer capacity")
        return self


class TextQuestion(ClarificationQuestionBase, Generic[QuestionAttributesT]):
    """Question answered by non-blank free text."""

    answer_type: Literal["text"] = "text"
    attributes: QuestionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )


class DateQuestion(ClarificationQuestionBase, Generic[QuestionAttributesT]):
    """Question answered by one ISO 8601 calendar date without a time zone."""

    answer_type: Literal["date"] = "date"
    attributes: QuestionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )


def _require_minute_precision(value: time, *, field: str) -> None:
    if value.tzinfo is not None:
        raise ValueError(f"{field} must not contain a UTC offset")
    if value.second != 0 or value.microsecond != 0:
        raise ValueError(f"{field} must use minute precision")


def _require_local_datetime_minute(value: datetime, *, field: str) -> None:
    if value.tzinfo is not None:
        raise ValueError(f"{field} must not contain a UTC offset")
    if value.second != 0 or value.microsecond != 0:
        raise ValueError(f"{field} must use minute precision")


def _resolve_local_datetime(
    value: datetime,
    zone: ZoneInfo,
    *,
    field: str,
) -> datetime:
    """Return one unique zoned instant or reject a DST gap or fold."""

    _require_local_datetime_minute(value, field=field)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        aware = value.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == value:
            candidates.setdefault(instant, aware)
    if not candidates:
        raise ValueError(f"{field} identifies a nonexistent local minute")
    if len(candidates) != 1:
        raise ValueError(f"{field} identifies an ambiguous local minute")
    return next(iter(candidates.values()))


class _TimeZoneQuestionBase(
    ClarificationQuestionBase,
    Generic[QuestionAttributesT],
):
    """Shared IANA time-zone contract for local temporal questions."""

    time_zone: ClarificationText = Field(
        description="IANA time zone used to interpret and display the local answer",
    )
    attributes: QuestionAttributesT | None = Field(
        default=None,
        description="Optional non-authoritative host metadata exposed to users",
    )

    @model_validator(mode="after")
    def time_zone_is_valid(
        self,
    ) -> _TimeZoneQuestionBase[QuestionAttributesT]:
        """Reject a zone that cannot resolve local answers to civil time."""

        try:
            ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("time_zone must be a valid IANA time zone") from error
        return self

    def _time_zone_info(self) -> ZoneInfo:
        """Return the zone already validated by the inherited model contract."""

        return ZoneInfo(self.time_zone)


class TimeQuestion(
    _TimeZoneQuestionBase[QuestionAttributesT],
    Generic[QuestionAttributesT],
):
    """Question answered by one local wall-clock minute in an IANA time zone."""

    answer_type: Literal["time"] = "time"
    minimum: time | None = Field(
        default=None,
        description="Optional inclusive local lower bound at minute precision",
    )
    maximum: time | None = Field(
        default=None,
        description="Optional inclusive local upper bound at minute precision",
    )

    @model_validator(mode="after")
    def time_contract_is_satisfiable(self) -> TimeQuestion[QuestionAttributesT]:
        """Require one non-wrapping minute range."""

        if self.minimum is not None:
            _require_minute_precision(self.minimum, field="minimum")
        if self.maximum is not None:
            _require_minute_precision(self.maximum, field="maximum")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum must be earlier than or equal to maximum")
        return self


class DateTimeQuestion(
    _TimeZoneQuestionBase[QuestionAttributesT],
    Generic[QuestionAttributesT],
):
    """Question answered by one unique local datetime in an IANA time zone."""

    answer_type: Literal["datetime"] = "datetime"
    minimum: datetime | None = Field(
        default=None,
        description="Optional inclusive local lower bound at minute precision",
    )
    maximum: datetime | None = Field(
        default=None,
        description="Optional inclusive local upper bound at minute precision",
    )

    @model_validator(mode="after")
    def datetime_contract_is_satisfiable(
        self,
    ) -> DateTimeQuestion[QuestionAttributesT]:
        """Require a real zone, unique local bounds, and an ordered instant range."""

        zone = self._time_zone_info()
        minimum = (
            None
            if self.minimum is None
            else _resolve_local_datetime(self.minimum, zone, field="minimum")
        )
        maximum = (
            None
            if self.maximum is None
            else _resolve_local_datetime(self.maximum, zone, field="maximum")
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("minimum must be earlier than or equal to maximum")
        return self


class ClarificationFormBase(ClarificationModel):
    """Core fields and invariants for a Plan clarification form.

    ``default_time_zone`` supplies the IANA zone copied into omitted ``timeZone``
    fields when a Definition binds ``TimeQuestion`` and ``DateTimeQuestion`` models.
    Individual questions can always declare a different zone explicitly.
    """

    default_time_zone: ClassVar[str] = "UTC"

    @model_validator(mode="after")
    def question_ids_are_unique(self) -> ClarificationFormBase:
        """Require a non-empty form with unambiguous question addressing."""

        value = getattr(self, "questions", None)
        if not isinstance(value, tuple):
            raise TypeError("Clarification form questions must be a tuple")
        items = cast(tuple[object, ...], value)
        if not all(
            isinstance(question, ClarificationQuestionBase) for question in items
        ):
            raise TypeError("Clarification form questions must contain questions")
        questions = cast(tuple[ClarificationQuestionBase, ...], items)
        question_ids = tuple(question.id for question in questions)
        if not question_ids:
            raise ValueError("Clarification forms must contain at least one question")
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("Clarification question IDs must be unique")
        return self


QuestionT = TypeVar("QuestionT", bound=ClarificationQuestionBase)


class ClarificationForm(ClarificationFormBase, Generic[QuestionT]):
    """Form specialized with one concrete question type or discriminated union."""

    questions: tuple[QuestionT, ...] = Field(min_length=1)


class BuiltInClarificationForm(
    ClarificationFormBase,
    Generic[QuestionAttributesT, OptionT],
):
    """Form exposing all built-in question types with shared typed metadata."""

    questions: tuple[
        Annotated[
            SingleChoiceQuestion[QuestionAttributesT, OptionT]
            | MultipleChoiceQuestion[QuestionAttributesT, OptionT]
            | TextQuestion[QuestionAttributesT]
            | DateQuestion[QuestionAttributesT]
            | TimeQuestion[QuestionAttributesT]
            | DateTimeQuestion[QuestionAttributesT],
            Field(discriminator="answer_type"),
        ],
        ...,
    ] = Field(min_length=1)


class DefaultClarificationForm(
    BuiltInClarificationForm[
        ClarificationModel,
        ClarificationOption[ClarificationModel],
    ]
):
    """Default form requiring no host-defined business models."""


BuiltInQuestion: TypeAlias = Annotated[
    SingleChoiceQuestion[ClarificationModel, ClarificationOption[ClarificationModel]]
    | MultipleChoiceQuestion[
        ClarificationModel, ClarificationOption[ClarificationModel]
    ]
    | TextQuestion[ClarificationModel]
    | DateQuestion[ClarificationModel]
    | TimeQuestion[ClarificationModel]
    | DateTimeQuestion[ClarificationModel],
    Field(discriminator="answer_type"),
]


class ClarificationResponseBase(ClarificationModel):
    """Framework-owned fields shared by every answered clarification payload."""

    status: Literal["answered"] = "answered"


class SingleChoiceResponse(ClarificationResponseBase):
    """Untrusted single-choice response using one option or one custom answer."""

    answer_type: Literal["single_choice"] = "single_choice"
    option_id: ClarificationId | None = None
    custom_answer: ClarificationText | None = None

    @model_validator(mode="after")
    def exactly_one_answer_path(self) -> SingleChoiceResponse:
        """Keep checkpoint selection and custom text mutually exclusive."""

        if (self.option_id is None) == (self.custom_answer is None):
            raise ValueError("single-choice response requires exactly one answer path")
        return self


class MultipleChoiceResponse(ClarificationResponseBase):
    """Untrusted multiple-choice response with options and optional custom text."""

    answer_type: Literal["multiple_choice"] = "multiple_choice"
    option_ids: tuple[ClarificationId, ...] = ()
    custom_answer: ClarificationText | None = None

    @model_validator(mode="after")
    def answer_paths_are_usable(self) -> MultipleChoiceResponse:
        """Require at least one answer path and reject duplicate option IDs."""

        if not self.option_ids and self.custom_answer is None:
            raise ValueError(
                "multiple-choice response requires at least one answer path"
            )
        if len(self.option_ids) != len(set(self.option_ids)):
            raise ValueError("multiple-choice option IDs must be unique")
        return self


class TextResponse(ClarificationResponseBase):
    """Untrusted non-blank text response."""

    answer_type: Literal["text"] = "text"
    answer: ClarificationText


class DateResponse(ClarificationResponseBase):
    """Untrusted calendar-date response serialized as ``YYYY-MM-DD``."""

    answer_type: Literal["date"] = "date"
    date: date


class TimeResponse(ClarificationResponseBase):
    """Untrusted local time response serialized at minute precision."""

    answer_type: Literal["time"] = "time"
    time: time

    @model_validator(mode="after")
    def answer_uses_local_minute_precision(self) -> TimeResponse:
        """Reject offsets and hidden seconds before checkpoint normalization."""

        _require_minute_precision(self.time, field="time")
        return self


class DateTimeResponse(ClarificationResponseBase):
    """Untrusted local datetime response serialized at minute precision."""

    answer_type: Literal["datetime"] = "datetime"
    date_time: datetime

    @model_validator(mode="after")
    def answer_uses_local_minute_precision(self) -> DateTimeResponse:
        """Reject offsets and hidden seconds before time-zone resolution."""

        _require_local_datetime_minute(self.date_time, field="date_time")
        return self


class SkippedResponse(ClarificationModel):
    """Explicit skip for one optional clarification question."""

    status: Literal["skipped"] = "skipped"


BuiltInResponse: TypeAlias = Annotated[
    SingleChoiceResponse
    | MultipleChoiceResponse
    | TextResponse
    | DateResponse
    | TimeResponse
    | DateTimeResponse,
    Field(discriminator="answer_type"),
]


__all__ = [
    "BuiltInClarificationForm",
    "BuiltInQuestion",
    "BuiltInResponse",
    "ClarificationForm",
    "ClarificationFormBase",
    "ClarificationId",
    "ClarificationModel",
    "ClarificationOption",
    "ClarificationOptionBase",
    "ClarificationQuestionBase",
    "ClarificationResponseBase",
    "ClarificationText",
    "ClarificationTypeId",
    "DateQuestion",
    "DateResponse",
    "DateTimeQuestion",
    "DateTimeResponse",
    "DefaultClarificationForm",
    "MultipleChoiceQuestion",
    "MultipleChoiceResponse",
    "SingleChoiceQuestion",
    "SingleChoiceResponse",
    "SkippedResponse",
    "TextQuestion",
    "TextResponse",
    "TimeQuestion",
    "TimeResponse",
]
