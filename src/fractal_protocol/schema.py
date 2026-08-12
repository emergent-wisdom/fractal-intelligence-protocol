from __future__ import annotations

import json
from typing import Any

from .errors import DomainError, require


SUPPORTED_KEYWORDS = {
    "type",
    "required",
    "properties",
    "items",
    "additionalProperties",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
}
JSON_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def validate_schema_definition(schema: Any, path: str = "$") -> dict[str, Any]:
    require(
        isinstance(schema, dict),
        "invalid_manifest_schema",
        f"Schema at {path} must be an object",
    )
    unsupported = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unsupported:
        raise DomainError(
            "invalid_manifest_schema",
            "The zero-dependency v1 validator does not support these schema keywords",
            details={"path": path, "keywords": unsupported},
        )
    schema_type = schema.get("type")
    if schema_type is not None:
        require(
            schema_type in JSON_TYPES,
            "invalid_manifest_schema",
            f"Unsupported schema type at {path}",
        )
    if "required" in schema:
        required = schema["required"]
        valid = (
            isinstance(required, list)
            and all(isinstance(item, str) and item for item in required)
            and len(set(required)) == len(required)
        )
        require(valid, "invalid_manifest_schema", f"required at {path} must contain unique strings")
    if "properties" in schema:
        properties = schema["properties"]
        require(
            isinstance(properties, dict),
            "invalid_manifest_schema",
            f"properties at {path} must be an object",
        )
        for name, child in properties.items():
            require(
                isinstance(name, str) and bool(name),
                "invalid_manifest_schema",
                f"property names at {path} must be non-empty strings",
            )
            validate_schema_definition(child, f"{path}/properties/{name}")
    if "items" in schema:
        validate_schema_definition(schema["items"], f"{path}/items")
    if "additionalProperties" in schema:
        require(
            isinstance(schema["additionalProperties"], bool),
            "invalid_manifest_schema",
            f"additionalProperties at {path} must be boolean",
        )
    if "enum" in schema:
        require(
            isinstance(schema["enum"], list) and bool(schema["enum"]),
            "invalid_manifest_schema",
            f"enum at {path} must be a non-empty list",
        )
    for keyword in ("minimum", "maximum"):
        if keyword in schema:
            value = schema[keyword]
            require(
                isinstance(value, (int, float)) and not isinstance(value, bool),
                "invalid_manifest_schema",
                f"{keyword} at {path} must be numeric",
            )
    for keyword in ("minLength", "maxLength"):
        if keyword in schema:
            value = schema[keyword]
            require(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                "invalid_manifest_schema",
                f"{keyword} at {path} must be a non-negative integer",
            )
    return schema


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def validate_instance(value: Any, schema: dict[str, Any], path: str = "$") -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        return [
            {
                "path": path,
                "code": "type",
                "message": f"Expected {expected_type}",
            }
        ]
    if "enum" in schema and not any(
        json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
        == json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        for item in schema["enum"]
    ):
        errors.append({"path": path, "code": "enum", "message": "Value is not in enum"})

    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(
                    {
                        "path": f"{path}/{name}",
                        "code": "required",
                        "message": "Required property is missing",
                    }
                )
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                errors.extend(validate_instance(item, properties[name], f"{path}/{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(
                    {
                        "path": f"{path}/{name}",
                        "code": "additional_property",
                        "message": "Additional property is not allowed",
                    }
                )
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate_instance(item, schema["items"], f"{path}/{index}"))
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append({"path": path, "code": "minLength", "message": "String is too short"})
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append({"path": path, "code": "maxLength", "message": "String is too long"})
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append({"path": path, "code": "minimum", "message": "Number is below minimum"})
        if "maximum" in schema and value > schema["maximum"]:
            errors.append({"path": path, "code": "maximum", "message": "Number is above maximum"})
    return errors
