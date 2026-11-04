"""One schema for provider Structured Outputs and local boundary validation."""

import copy


def _text(limit=1200):
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _enum(*values):
    return {"type": "string", "enum": list(values)}


def _array(items, maximum, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


def _object(**properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_SCHEMA = _object(
    classification=_enum("general_bug", "security_suspected", "needs_review"),
    confidence=_enum("low", "medium", "high"),
    classification_reason=_text(),
    severity=_enum("unknown", "low", "medium", "high", "critical"),
    severity_reason=_text(),
    summary=_text(),
    hypotheses=_array(_object(description=_text(), source_refs=_array(_text(150), 3, 1)), 3),
    solutions=_array(_object(
        title=_text(200), description=_text(2000), source_refs=_array(_text(150), 3, 1),
        verification_steps=_array(_text(800), 6, 1), tradeoffs=_array(_text(800), 4),
    ), 3, 1),
    missing_information=_array(_text(800), 6),
)


def analysis_schema(source_refs: set[str]) -> dict:
    """Only this request's evidence references may appear in generated proposals."""
    schema = copy.deepcopy(_SCHEMA)
    for key in ("hypotheses", "solutions"):
        schema["properties"][key]["items"]["properties"]["source_refs"]["items"] = _enum(*sorted(source_refs))
    return schema


def validate_output(value, schema: dict) -> None:
    """Validate only the JSON Schema subset defined above; not a general validator."""
    kind = schema["type"]
    if kind == "object":
        properties = schema["properties"]
        if type(value) is not dict or set(value) != set(properties):
            raise ValueError("incorrect object fields")
        for key, child in properties.items():
            validate_output(value[key], child)
    elif kind == "array":
        if type(value) is not list or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise ValueError("incorrect array size")
        for item in value:
            validate_output(item, schema["items"])
    elif kind == "string":
        if type(value) is not str or not value.strip():
            raise ValueError("expected nonempty text")
        value.encode("utf-8")
        if "enum" in schema:
            if value not in schema["enum"]:
                raise ValueError("invalid enum value")
        elif not schema["minLength"] <= len(value) <= schema["maxLength"]:
            raise ValueError("incorrect text length")
    else:
        raise ValueError("unsupported schema")
