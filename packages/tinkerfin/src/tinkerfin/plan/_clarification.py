"""Definition-bound clarification contracts, persistence, and response validation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType, NoneType
from typing import Annotated, Any, Literal, cast, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, create_model

from ._json_schema import require_valid_schema, validate_json_schema_instance
from .clarification import (
    ClarificationFormBase,
    ClarificationModel,
    ClarificationOptionBase,
    ClarificationQuestionBase,
    DateQuestion,
    DateTimeQuestion,
    MultipleChoiceQuestion,
    MultipleChoiceResponse,
    SingleChoiceQuestion,
    SingleChoiceResponse,
    SkippedResponse,
    TextQuestion,
    TimeQuestion,
)
from .clarification_types import (
    BUILTIN_CLARIFICATION_TYPES,
    ClarificationType,
    _validate_descriptor,
)
from .errors import (
    PlanClarificationResponseError,
    PlanModeConfigurationError,
    PlanStructuredOutputError,
)
from .models import RequirementAnswer

_JSON_OBJECT = TypeAdapter(
    dict[str, JsonValue],
    config=ConfigDict(allow_inf_nan=False),
)
_NON_BLANK_PATTERN = r"\S"
_BUILTIN_IDS = frozenset(item.type_id for item in BUILTIN_CLARIFICATION_TYPES)
_BUILTIN_QUESTION_BASES: dict[str, type[ClarificationQuestionBase]] = {
    "single_choice": SingleChoiceQuestion,
    "multiple_choice": MultipleChoiceQuestion,
    "text": TextQuestion,
    "date": DateQuestion,
    "time": TimeQuestion,
    "datetime": DateTimeQuestion,
}


@dataclass(frozen=True, slots=True)
class ClarificationQuestionCount:
    """Model-visible question count bounds derived from one host form schema."""

    minimum: int
    maximum: int | None


@dataclass(frozen=True, slots=True)
class ClarificationSchemaBinding:
    """Complete immutable clarification contract frozen for one Definition."""

    form_schema: type[ClarificationFormBase]
    fingerprint: str
    question_count: ClarificationQuestionCount
    types: Mapping[str, ClarificationType[Any, Any]]
    question_models: Mapping[str, type[ClarificationQuestionBase]]


def _form_questions(
    form: ClarificationFormBase,
) -> tuple[ClarificationQuestionBase, ...]:
    value = getattr(form, "questions", None)
    if not isinstance(value, tuple):
        raise TypeError("clarification form questions must be a concrete tuple")
    items = cast(tuple[object, ...], value)
    if not all(isinstance(question, ClarificationQuestionBase) for question in items):
        raise TypeError("clarification form questions must be a concrete tuple")
    return cast(tuple[ClarificationQuestionBase, ...], items)


def _question_answer_type(question: ClarificationQuestionBase) -> str:
    value = getattr(question, "answer_type", None)
    if not isinstance(value, str) or not value:
        raise TypeError("clarification question must have one answer_type")
    return value


def _response_answer_type(response: BaseModel) -> str:
    value = getattr(response, "answer_type", None)
    if not isinstance(value, str) or not value:
        raise TypeError("clarification response must have one answer_type")
    return value


def _annotation_leaves(annotation: object) -> tuple[object, ...]:
    origin = get_origin(annotation)
    if origin is Annotated:
        arguments = get_args(annotation)
        return _annotation_leaves(arguments[0]) if arguments else ()
    arguments = get_args(annotation)
    if arguments:
        leaves: list[object] = []
        for argument in arguments:
            if argument is Ellipsis:
                continue
            leaves.extend(_annotation_leaves(argument))
        return tuple(leaves)
    return (annotation,)


def _require_concrete_model(model: type[BaseModel], *, source: str) -> None:
    parameters = getattr(model, "__parameters__", ())
    if parameters:
        raise PlanModeConfigurationError(f"{source} must not contain unbound generics")


def _require_inherited_core_fields(
    model: type[BaseModel],
    *,
    base: type[BaseModel],
    field_names: frozenset[str],
    source: str,
) -> None:
    """Prevent host models from weakening framework-owned field contracts."""

    for parent in model.__mro__:
        if parent is base:
            return
        annotations = vars(parent).get("__annotations__", {})
        overridden = field_names.intersection(annotations)
        if overridden:
            fields = ", ".join(repr(name) for name in sorted(overridden))
            raise PlanModeConfigurationError(
                f"{source} must inherit framework core fields unchanged: {fields}"
            )
    raise PlanModeConfigurationError(f"{source} must inherit from {base.__name__}")


def _field_model_types(
    model: type[BaseModel],
    field_name: str,
    *,
    expected: type[BaseModel],
    source: str,
    optional: bool = False,
    variadic_tuple: bool = False,
) -> tuple[type[BaseModel], ...]:
    """Extract concrete model leaves from one framework-owned field contract.

    The helper rejects ``Any`` and abstract placeholders so persisted forms cannot
    depend on a runtime subtype the Definition did not freeze. Variadic tuple checks
    also preserve repeated question and option Schema semantics.
    """

    field = model.model_fields.get(field_name)
    if field is None:
        if optional:
            return ()
        raise PlanModeConfigurationError(f"{source} must define {field_name!r}")
    if variadic_tuple:
        arguments = get_args(field.annotation)
        if (
            get_origin(field.annotation) is not tuple
            or len(arguments) != 2
            or arguments[1] is not Ellipsis
        ):
            raise PlanModeConfigurationError(
                f"{source}.{field_name} must be a variadic tuple"
            )
    found: list[type[BaseModel]] = []
    for leaf in _annotation_leaves(field.annotation):
        if optional and leaf is NoneType:
            continue
        if leaf is Any or not isinstance(leaf, type) or not issubclass(leaf, expected):
            raise PlanModeConfigurationError(
                f"{source}.{field_name} must contain concrete {expected.__name__} types"
            )
        _require_concrete_model(leaf, source=f"{source}.{field_name}")
        found.append(leaf)
    if not found and not optional:
        raise PlanModeConfigurationError(
            f"{source}.{field_name} must contain at least one {expected.__name__}"
        )
    return tuple(dict.fromkeys(found))


def _validate_attributes(model: type[BaseModel], *, source: str) -> None:
    _field_model_types(
        model,
        "attributes",
        expected=ClarificationModel,
        source=source,
        optional=True,
    )


def _answer_type_const(model: type[BaseModel], *, source: str) -> str:
    try:
        schema = _JSON_OBJECT.validate_python(model.model_json_schema(by_alias=True))
    except Exception as error:
        raise PlanModeConfigurationError(
            f"{source} could not produce a JSON Schema",
            cause=error,
        ) from error
    properties = schema.get("properties")
    answer_type = (
        cast(Mapping[str, JsonValue], properties).get("answerType")
        if isinstance(properties, Mapping)
        else None
    )
    if not isinstance(answer_type, Mapping) or not isinstance(
        answer_type.get("const"), str
    ):
        raise PlanModeConfigurationError(
            f"{source}.answer_type must be one string Literal"
        )
    return cast(str, answer_type["const"])


def _validate_questions_json_schema(
    model: type[ClarificationFormBase], *, source: str
) -> ClarificationQuestionCount:
    """Require model-visible, satisfiable clarification cardinality constraints."""

    field = model.model_fields["questions"]
    if not field.is_required():
        raise PlanModeConfigurationError(f"{source}.questions must be required")
    try:
        schema = _JSON_OBJECT.validate_python(model.model_json_schema(by_alias=True))
    except Exception as error:
        raise PlanModeConfigurationError(
            f"{source} could not produce a JSON Schema",
            cause=error,
        ) from error
    properties_value = schema.get("properties")
    properties = (
        cast(Mapping[str, JsonValue], properties_value)
        if isinstance(properties_value, Mapping)
        else None
    )
    questions_value = properties.get("questions") if properties is not None else None
    if (
        not isinstance(questions_value, Mapping)
        or questions_value.get("type") != "array"
    ):
        raise PlanModeConfigurationError(
            f"{source}.questions must expose the stable JSON array field 'questions'"
        )
    minimum = questions_value.get("minItems")
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise PlanModeConfigurationError(
            f"{source}.questions JSON Schema must declare minItems >= 1"
        )
    maximum = questions_value.get("maxItems")
    if maximum is not None and (
        isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < minimum
    ):
        raise PlanModeConfigurationError(
            f"{source}.questions JSON Schema maxItems must be >= minItems"
        )
    return ClarificationQuestionCount(minimum=minimum, maximum=maximum)


def _validate_question_model(
    question_type: type[ClarificationQuestionBase],
    *,
    source: str,
) -> str:
    """Validate inherited core fields and return one stable answer-type identity."""

    _require_inherited_core_fields(
        question_type,
        base=ClarificationQuestionBase,
        field_names=frozenset({"id", "prompt", "required"}),
        source=source,
    )
    type_id = _answer_type_const(question_type, source=source)
    _validate_attributes(question_type, source=source)
    if issubclass(question_type, (SingleChoiceQuestion, MultipleChoiceQuestion)):
        option_types = _field_model_types(
            cast(type[BaseModel], question_type),
            "options",
            expected=ClarificationOptionBase,
            source=source,
            variadic_tuple=True,
        )
        for option_type in option_types:
            _require_inherited_core_fields(
                option_type,
                base=ClarificationOptionBase,
                field_names=frozenset({"description", "id", "label"}),
                source=option_type.__name__,
            )
            _validate_attributes(option_type, source=option_type.__name__)
    return type_id


def _validate_form_schema(
    value: object,
) -> tuple[
    type[ClarificationFormBase],
    ClarificationQuestionCount,
    dict[str, type[ClarificationQuestionBase]],
]:
    """Freeze one concrete form and its unique question-type dispatch table.

    Model annotations and generated JSON Schema are checked together; accepting only
    one of them would let runtime parsing and published response constraints diverge.
    """

    if not isinstance(value, type) or not issubclass(value, ClarificationFormBase):
        raise PlanModeConfigurationError(
            "clarification_schema must be a ClarificationFormBase subclass"
        )
    _require_concrete_model(value, source="clarification_schema")
    raw_question_types = _field_model_types(
        value,
        "questions",
        expected=ClarificationQuestionBase,
        source="clarification_schema",
        variadic_tuple=True,
    )
    question_count = _validate_questions_json_schema(
        value,
        source="clarification_schema",
    )
    question_models: dict[str, type[ClarificationQuestionBase]] = {}
    for raw_type in raw_question_types:
        question_type = cast(type[ClarificationQuestionBase], raw_type)
        type_id = _validate_question_model(
            question_type,
            source=question_type.__name__,
        )
        if type_id in question_models:
            raise PlanModeConfigurationError(
                f"clarification form contains duplicate answer_type: {type_id}"
            )
        question_models[type_id] = question_type
    return value, question_count, question_models


def _compose_form_schema(
    form_schema: type[ClarificationFormBase],
    question_count: ClarificationQuestionCount,
    question_models: Sequence[type[ClarificationQuestionBase]],
) -> type[ClarificationFormBase]:
    union: Any = question_models[0]
    for question_model in question_models[1:]:
        union = union | question_model
    question = Annotated[union, Field(discriminator="answer_type")]
    annotation = tuple[question, ...]
    bound = create_model(
        f"{form_schema.__name__}Bound",
        __base__=form_schema,
        __module__=form_schema.__module__,
        questions=(
            annotation,
            Field(
                min_length=question_count.minimum,
                max_length=question_count.maximum,
                description="Questions that must each be answered or explicitly skipped",
            ),
        ),
    )
    return bound


def _validated_default_time_zone(
    form_schema: type[ClarificationFormBase],
) -> str:
    """Freeze one valid IANA default before publishing a Definition contract."""

    value = form_schema.default_time_zone
    if not isinstance(value, str):
        raise PlanModeConfigurationError(
            "clarification_schema.default_time_zone must be a valid IANA time zone"
        )
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise PlanModeConfigurationError(
            "clarification_schema.default_time_zone must be a valid IANA time zone",
            cause=error,
        ) from error
    return value


def _bind_question_default_time_zone(
    question_model: type[ClarificationQuestionBase],
    *,
    default_time_zone: str,
) -> type[ClarificationQuestionBase]:
    """Publish a form-owned zone as the actual question field default.

    A concrete derived model is necessary because Pydantic otherwise keeps the
    generic question's required field in the Planner JSON Schema. Copying the complete
    ``FieldInfo`` preserves host constraints, aliases, and descriptions while making
    the effective default visible to structured-output providers and fingerprints.
    """

    field = question_model.model_fields.get("time_zone")
    if field is None:
        raise PlanModeConfigurationError(
            "built-in time questions must define the framework time_zone field"
        )
    if field.default == default_time_zone:
        return question_model
    bound_field = deepcopy(field)
    bound_field.default = default_time_zone
    bound_field.default_factory = None
    bound_field.validate_default = True
    bound = create_model(
        f"{question_model.__name__}WithFormDefault",
        __base__=question_model,
        __module__=question_model.__module__,
        __doc__=question_model.__doc__,
        time_zone=(field.annotation, bound_field),
    )
    return bound


def _schema_fingerprint(value: object) -> str:
    payload: object = value
    if isinstance(value, type) and issubclass(value, BaseModel):
        payload = value.model_json_schema(by_alias=True)
    try:
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise PlanModeConfigurationError(
            "clarification contract must contain canonical finite JSON",
            cause=error,
        ) from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_clarification_binding(
    schema: object,
    *,
    custom_types: Sequence[ClarificationType[Any, Any]] = (),
) -> ClarificationSchemaBinding:
    """Create one immutable complete clarification contract for a Definition."""

    form_schema, question_count, question_models = _validate_form_schema(schema)
    default_time_zone = _validated_default_time_zone(form_schema)
    descriptors: dict[str, ClarificationType[Any, Any]] = {
        item.type_id: item for item in BUILTIN_CLARIFICATION_TYPES
    }
    for descriptor in custom_types:
        if not isinstance(descriptor, ClarificationType):
            raise TypeError("clarification_types must contain ClarificationType values")
        _validate_descriptor(descriptor, allow_builtin=False)
        if descriptor.type_id in descriptors:
            raise PlanModeConfigurationError(
                f"clarification type is already registered: {descriptor.type_id}"
            )
        descriptors[descriptor.type_id] = descriptor
        question_models.setdefault(descriptor.type_id, descriptor.question_model)

    for type_id, question_model in question_models.items():
        descriptor = descriptors.get(type_id)
        if descriptor is None:
            raise PlanModeConfigurationError(
                f"clarification form contains an unregistered answer_type: {type_id}"
            )
        expected = (
            _BUILTIN_QUESTION_BASES[type_id]
            if type_id in _BUILTIN_IDS
            else descriptor.question_model
        )
        if not issubclass(question_model, expected):
            raise PlanModeConfigurationError(
                f"question model does not match clarification type: {type_id}"
            )

    original_question_models = dict(question_models)
    for type_id in ("time", "datetime"):
        question_model = question_models.get(type_id)
        if question_model is not None:
            question_models[type_id] = _bind_question_default_time_zone(
                question_model,
                default_time_zone=default_time_zone,
            )

    if custom_types or question_models != original_question_models:
        form_schema = _compose_form_schema(
            form_schema,
            question_count,
            tuple(question_models.values()),
        )
        form_schema, question_count, question_models = _validate_form_schema(
            form_schema
        )

    reachable = {type_id: descriptors[type_id] for type_id in question_models}
    fingerprint_payload = {
        "domain": "tinkerfin.plan-clarification-binding",
        "form": form_schema.model_json_schema(by_alias=True),
        "types": [
            {
                "typeId": type_id,
                "description": reachable[type_id].description,
                "question": question_models[type_id].model_json_schema(by_alias=True),
                "response": reachable[type_id].response_model.model_json_schema(
                    by_alias=True
                ),
            }
            for type_id in sorted(reachable)
        ],
    }
    return ClarificationSchemaBinding(
        form_schema=form_schema,
        fingerprint=_schema_fingerprint(fingerprint_payload),
        question_count=question_count,
        types=MappingProxyType(dict(reachable)),
        question_models=MappingProxyType(dict(question_models)),
    )


def serialize_form(
    binding: ClarificationSchemaBinding,
    value: object,
) -> tuple[ClarificationFormBase, dict[str, JsonValue]]:
    """Validate and round-trip a model result before durable persistence."""

    if not isinstance(value, binding.form_schema):
        raise PlanStructuredOutputError(
            "structured clarification did not use the configured form schema"
        )
    try:
        payload = _JSON_OBJECT.validate_python(
            value.model_dump(mode="json", by_alias=True, exclude_none=False)
        )
        restored = binding.form_schema.model_validate(payload)
    except Exception as error:
        raise PlanStructuredOutputError(
            "structured clarification failed its JSON checkpoint round-trip",
            cause=error,
        ) from error
    return restored, payload


def restore_form(
    binding: ClarificationSchemaBinding,
    payload: object,
) -> ClarificationFormBase:
    """Restore one checkpoint form with the current Definition contract."""

    try:
        return binding.form_schema.model_validate(payload)
    except Exception as error:
        raise PlanModeConfigurationError(
            "checkpoint clarification form is invalid for this Definition",
            cause=error,
        ) from error


def _string_schema() -> dict[str, JsonValue]:
    return {"type": "string", "minLength": 1, "pattern": _NON_BLANK_PATTERN}


def _answered_schema(question: ClarificationQuestionBase) -> dict[str, JsonValue]:
    """Build the exact answered branch for one built-in checkpoint question.

    Choice IDs come from the trusted frozen form. ``oneOf`` expresses the exclusive
    custom-answer path, and multiple-choice bounds count a custom answer as one
    selection so UI order cannot alter validation.
    """

    type_id = _question_answer_type(question)
    base: dict[str, JsonValue] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "answerType"],
        "properties": {
            "status": {"const": "answered"},
            "answerType": {"const": type_id},
        },
    }
    properties = cast(dict[str, JsonValue], base["properties"])
    required = cast(list[JsonValue], base["required"])
    if isinstance(question, SingleChoiceQuestion):
        single = cast(
            SingleChoiceQuestion[ClarificationModel, ClarificationOptionBase],
            question,
        )
        properties["optionId"] = {
            "type": "string",
            "enum": [option.id for option in single.options],
        }
        if single.allow_free_text:
            properties["customAnswer"] = _string_schema()
            base["oneOf"] = [
                {
                    "required": ["optionId"],
                    "not": {"required": ["customAnswer"]},
                },
                {
                    "required": ["customAnswer"],
                    "not": {"required": ["optionId"]},
                },
            ]
        else:
            required.append("optionId")
    elif isinstance(question, MultipleChoiceQuestion):
        multiple = cast(
            MultipleChoiceQuestion[ClarificationModel, ClarificationOptionBase],
            question,
        )
        option_ids: dict[str, JsonValue] = {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [option.id for option in multiple.options],
            },
            "uniqueItems": True,
        }
        properties["optionIds"] = option_ids
        required.append("optionIds")
        maximum = multiple.max_selections or (
            len(multiple.options) + int(multiple.allow_free_text)
        )
        if multiple.allow_free_text:
            properties["customAnswer"] = _string_schema()
            base["oneOf"] = [
                {
                    "required": ["customAnswer"],
                    "properties": {
                        "optionIds": {
                            **option_ids,
                            "minItems": max(0, multiple.min_selections - 1),
                            "maxItems": max(0, maximum - 1),
                        }
                    },
                },
                {
                    "not": {"required": ["customAnswer"]},
                    "properties": {
                        "optionIds": {
                            **option_ids,
                            "minItems": multiple.min_selections,
                            "maxItems": maximum,
                        }
                    },
                },
            ]
        else:
            option_ids["minItems"] = multiple.min_selections
            option_ids["maxItems"] = maximum
    elif isinstance(question, TextQuestion):
        properties["answer"] = _string_schema()
        required.append("answer")
    elif isinstance(question, DateQuestion):
        properties["date"] = {"type": "string", "format": "date"}
        required.append("date")
    elif isinstance(question, TimeQuestion):
        properties["time"] = {
            "type": "string",
            "format": "time",
            "pattern": r"^(?:[01][0-9]|2[0-3]):[0-5][0-9](?::00)?$",
        }
        required.append("time")
    elif isinstance(question, DateTimeQuestion):
        properties["dateTime"] = {
            "type": "string",
            "pattern": (
                r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
                r"(?:[01][0-9]|2[0-3]):[0-5][0-9](?::00)?$"
            ),
        }
        required.append("dateTime")
    else:
        raise TypeError(
            "custom clarification questions require their registered Schema"
        )
    return base


def _rewrite_schema_refs(
    value: JsonValue,
    *,
    renames: Mapping[str, str],
) -> JsonValue:
    if isinstance(value, list):
        return [_rewrite_schema_refs(item, renames=renames) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten: dict[str, JsonValue] = {}
    for key, child in value.items():
        if key == "$ref" and isinstance(child, str) and child.startswith("#/$defs/"):
            pointer = child.removeprefix("#/$defs/")
            original = pointer.replace("~1", "/").replace("~0", "~")
            renamed = renames.get(original)
            if renamed is None:
                rewritten[key] = child
            else:
                escaped = renamed.replace("~", "~0").replace("/", "~1")
                rewritten[key] = f"#/$defs/{escaped}"
        elif key != "$defs":
            rewritten[key] = _rewrite_schema_refs(child, renames=renames)
    return rewritten


def _scope_custom_schema(
    schema: dict[str, JsonValue],
    *,
    type_id: str,
    question_id: str,
    definitions: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    scope = hashlib.sha256(f"{type_id}\0{question_id}".encode()).hexdigest()
    prefix = f"custom_{scope}"
    raw_defs = schema.get("$defs")
    local_defs = (
        cast(dict[str, JsonValue], raw_defs) if isinstance(raw_defs, dict) else {}
    )
    renames = {name: f"{prefix}_{name}" for name in local_defs}
    for name, child in local_defs.items():
        scoped_name = renames[name]
        if scoped_name in definitions:
            raise PlanModeConfigurationError(
                "custom clarification response Schema definition collision"
            )
        definitions[scoped_name] = _rewrite_schema_refs(child, renames=renames)
    return cast(dict[str, JsonValue], _rewrite_schema_refs(schema, renames=renames))


class ClarificationDismissResponse(ClarificationModel):
    """Close a questionnaire without answering it or invoking the model."""

    type: Literal["dismiss"]


class ClarificationDiscussionResponse(ClarificationModel):
    """End the pending questionnaire and discuss it without submitting answers."""

    type: Literal["discuss"]
    message: str = Field(
        min_length=1,
        pattern=_NON_BLANK_PATTERN,
        description="User message about the pending clarification",
    )


def build_response_schema(
    binding: ClarificationSchemaBinding,
    form: ClarificationFormBase,
) -> dict[str, JsonValue]:
    """Build the exact Draft 2020-12 response Schema for one concrete form."""

    answers: dict[str, JsonValue] = {}
    definitions: dict[str, JsonValue] = {}
    for question in _form_questions(form):
        type_id = _question_answer_type(question)
        descriptor = binding.types[type_id]
        if type_id in _BUILTIN_IDS:
            answered = _answered_schema(question)
        else:
            raw = (
                descriptor.bind_response_schema(question)
                if descriptor.bind_response_schema is not None
                else descriptor.response_model.model_json_schema(by_alias=True)
            )
            scoped = _scope_custom_schema(
                _JSON_OBJECT.validate_python(raw),
                type_id=type_id,
                question_id=question.id,
                definitions=definitions,
            )
            answered = {
                "allOf": [
                    scoped,
                    {
                        "type": "object",
                        "required": ["status", "answerType"],
                        "properties": {
                            "status": {"const": "answered"},
                            "answerType": {"const": type_id},
                        },
                    },
                ]
            }
        answers[question.id] = cast(
            JsonValue,
            answered
            if question.required
            else {
                "oneOf": [
                    answered,
                    SkippedResponse.model_json_schema(by_alias=True),
                ]
            },
        )
    schema: dict[str, JsonValue] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["type"],
        "oneOf": [
            {
                "properties": {"type": {"const": "respond"}},
                "required": ["answers"],
                "not": {"required": ["message"]},
            },
            {
                "properties": {"type": {"const": "discuss"}},
                "required": ["message"],
                "not": {"required": ["answers"]},
            },
            {
                "properties": {"type": {"const": "dismiss"}},
                "not": {
                    "anyOf": [{"required": ["answers"]}, {"required": ["message"]}]
                },
            },
        ],
        "properties": {
            "type": {"enum": ["respond", "discuss", "dismiss"]},
            # Resume validation must reject blank discussion before saving acceptance.
            "message": {
                "type": "string",
                "minLength": 1,
                "pattern": _NON_BLANK_PATTERN,
            },
            "answers": {
                "type": "object",
                "additionalProperties": False,
                "required": [question.id for question in _form_questions(form)],
                "properties": answers,
            },
        },
    }
    if definitions:
        schema["$defs"] = definitions
    require_valid_schema(schema)
    return schema


def pending_contract_digest(
    form: Mapping[str, JsonValue],
    response_schema: Mapping[str, JsonValue],
) -> str:
    """Return the exact canonical digest for one pending clarification."""

    return _schema_fingerprint(
        {
            "domain": "tinkerfin.plan-clarification-pending",
            "form": dict(form),
            "responseSchema": dict(response_schema),
        }
    )


def _validate_builtin_response(
    question: ClarificationQuestionBase,
    response: BaseModel,
) -> None:
    """Recheck a parsed built-in response against its exact checkpoint question.

    JSON Schema rejects malformed transport input; this second boundary enforces the
    trusted domain relation to the specific options and selection limits stored in the
    checkpoint before normalized answers enter Plan context.
    """

    if isinstance(question, SingleChoiceQuestion):
        single = cast(
            SingleChoiceQuestion[ClarificationModel, ClarificationOptionBase],
            question,
        )
        if not isinstance(response, SingleChoiceResponse):
            raise TypeError("single-choice question received a different answer type")
        if response.option_id is not None and response.option_id not in {
            option.id for option in single.options
        }:
            raise ValueError("single-choice response selected an unknown option")
        if response.custom_answer is not None and not single.allow_free_text:
            raise ValueError("single-choice question does not allow a custom answer")
    elif isinstance(question, MultipleChoiceQuestion):
        multiple = cast(
            MultipleChoiceQuestion[ClarificationModel, ClarificationOptionBase],
            question,
        )
        if not isinstance(response, MultipleChoiceResponse):
            raise TypeError("multiple-choice question received a different answer type")
        known = {option.id for option in multiple.options}
        if not set(response.option_ids) <= known:
            raise ValueError("multiple-choice response selected an unknown option")
        if response.custom_answer is not None and not multiple.allow_free_text:
            raise ValueError("multiple-choice question does not allow a custom answer")
        count = len(response.option_ids) + int(response.custom_answer is not None)
        maximum = multiple.max_selections or (
            len(multiple.options) + int(multiple.allow_free_text)
        )
        if count < multiple.min_selections or count > maximum:
            raise ValueError("multiple-choice response violates selection bounds")


def validate_clarification_response(
    binding: ClarificationSchemaBinding,
    form: ClarificationFormBase,
    response_schema: dict[str, JsonValue],
    value: object,
) -> (
    tuple[RequirementAnswer, ...]
    | ClarificationDiscussionResponse
    | ClarificationDismissResponse
):
    """Validate either a complete answer batch or an explicit discussion request."""
    if not isinstance(value, Mapping):
        return validate_and_normalize_response(binding, form, response_schema, value)
    response = cast(Mapping[object, object], value)
    if response.get("type") in {"discuss", "dismiss"}:
        try:
            payload = _JSON_OBJECT.validate_python(response)
            validate_json_schema_instance(payload, response_schema)
            return (
                ClarificationDismissResponse.model_validate(payload)
                if payload["type"] == "dismiss"
                else ClarificationDiscussionResponse.model_validate(payload)
            )
        except Exception as error:
            raise PlanClarificationResponseError(
                "discussion response does not match the pending form",
                cause=error,
            ) from error
    return validate_and_normalize_response(binding, form, response_schema, response)


def validate_and_normalize_response(
    binding: ClarificationSchemaBinding,
    form: ClarificationFormBase,
    response_schema: dict[str, JsonValue],
    value: object,
) -> tuple[RequirementAnswer, ...]:
    """Validate one complete response and return deterministic trusted answers."""

    try:
        payload = _JSON_OBJECT.validate_python(value)
        validate_json_schema_instance(payload, response_schema)
        raw_answers = payload.get("answers")
        if not isinstance(raw_answers, Mapping):
            raise TypeError("clarification response answers must be an object")
        answers = cast(Mapping[str, object], raw_answers)
        normalized: list[RequirementAnswer] = []
        for question in _form_questions(form):
            type_id = _question_answer_type(question)
            raw = answers[question.id]
            raw_mapping = (
                cast(Mapping[object, object], raw) if isinstance(raw, Mapping) else None
            )
            if raw_mapping is not None and raw_mapping.get("status") == "skipped":
                if question.required:
                    raise ValueError(
                        "required clarification question cannot be skipped"
                    )
                normalized.append(
                    RequirementAnswer(
                        question_id=question.id,
                        answer_type=type_id,
                        skipped=True,
                    )
                )
                continue
            descriptor = binding.types[type_id]
            response = descriptor.response_model.model_validate(raw)
            if _response_answer_type(response) != type_id:
                raise ValueError("clarification response answer type does not match")
            _validate_builtin_response(question, response)
            if descriptor.validate is not None:
                descriptor.validate(question, response)
            trusted = _JSON_OBJECT.validate_python(
                descriptor.normalize(question, response)
            )
            normalized.append(
                RequirementAnswer(
                    question_id=question.id,
                    answer_type=type_id,
                    value=trusted,
                )
            )
        return tuple(normalized)
    except PlanClarificationResponseError:
        raise
    except Exception as error:
        raise PlanClarificationResponseError(
            "clarification response does not match the pending form",
            cause=error,
        ) from error


__all__ = [
    "ClarificationSchemaBinding",
    "build_response_schema",
    "create_clarification_binding",
    "pending_contract_digest",
    "restore_form",
    "serialize_form",
    "validate_and_normalize_response",
    "validate_clarification_response",
]
