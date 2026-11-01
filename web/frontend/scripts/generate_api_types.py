#!/usr/bin/env python3
"""Generate TypeScript API contracts from the FastAPI application's live schema."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
OUTPUT = Path(__file__).resolve().parents[1] / "src/api/generated/openapi.ts"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _key(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _identifier(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_$]", "_", value)
    if not result or result[0].isdigit():
        result = f"_{result}"
    return result


def schema_type(schema: dict[str, Any] | bool | None) -> str:
    """Render the JSON Schema subset emitted by Pydantic/FastAPI."""
    if schema is None or schema is True:
        return "unknown"
    if schema is False:
        return "never"
    if "$ref" in schema:
        prefix = "#/components/schemas/"
        ref = schema["$ref"]
        if ref.startswith(prefix):
            return f"components[\"schemas\"][{_key(ref[len(prefix):])}]"
        return "unknown"
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    if "enum" in schema:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"]) or "never"
    for union_key in ("oneOf", "anyOf"):
        if union_key in schema:
            return " | ".join(f"({schema_type(part)})" for part in schema[union_key]) or "never"
    if "allOf" in schema:
        return " & ".join(f"({schema_type(part)})" for part in schema["allOf"]) or "unknown"

    schema_type_value = schema.get("type")
    if isinstance(schema_type_value, list):
        return " | ".join(_primitive_type(item, schema) for item in schema_type_value)
    return _primitive_type(schema_type_value, schema)


def _primitive_type(kind: str | None, schema: dict[str, Any]) -> str:
    if kind == "null":
        return "null"
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        item = schema_type(schema.get("items"))
        return f"Array<{item}>"
    if kind == "object" or "properties" in schema or "additionalProperties" in schema:
        properties = schema.get("properties", {})
        required = set(schema.get("required", []))
        rows = ["{"]
        for name, prop_schema in properties.items():
            optional = "" if name in required else "?"
            rows.append(f"  {_key(name)}{optional}: {schema_type(prop_schema)};")
        additional = schema.get("additionalProperties")
        if additional is not None and additional is not False:
            value_type = "unknown" if additional is True else schema_type(additional)
            rows.append(f"  [key: string]: {value_type};")
        rows.append("}")
        return "\n".join(rows)
    return "unknown"


def _media_schema(container: dict[str, Any] | None) -> dict[str, Any] | None:
    if not container:
        return None
    content = container.get("content", {})
    for media_type in ("application/json", "application/problem+json"):
        schema = content.get(media_type, {}).get("schema")
        if schema is not None:
            return schema
    for media in content.values():
        schema = media.get("schema")
        if schema is not None:
            return schema
    return None


def render_operation(operation: dict[str, Any]) -> str:
    rows = ["{"]
    params = operation.get("parameters", [])
    if params:
        by_location: dict[str, list[dict[str, Any]]] = {}
        for parameter in params:
            by_location.setdefault(parameter.get("in", "query"), []).append(parameter)
        for location in ("path", "query", "header", "cookie"):
            entries = by_location.get(location, [])
            if not entries:
                continue
            required = {item["name"] for item in entries if item.get("required")}
            rows.append(f"  {location}: {{")
            for item in entries:
                name = item.get("name", "")
                optional = "" if name in required else "?"
                rows.append(f"    {_key(name)}{optional}: {schema_type(item.get('schema'))};")
            rows.append("  };")
    request_schema = _media_schema(operation.get("requestBody"))
    if request_schema is not None:
        request_required = bool(operation.get("requestBody", {}).get("required"))
        optional = "" if request_required else "?"
        rows.append(f"  requestBody{optional}: {schema_type(request_schema)};")
    responses = operation.get("responses", {})
    if responses:
        rows.append("  responses: {")
        for status, response in responses.items():
            response_schema = _media_schema(response)
            rows.append(f"    {_key(status)}: {{")
            if response_schema is not None:
                rows.append(f"      content: {schema_type(response_schema)};")
            rows.append("    };")
        rows.append("  };")
    rows.append("}")
    return "\n".join(rows)


def render_openapi_types(document: dict[str, Any]) -> str:
    schemas = document.get("components", {}).get("schemas", {})
    operations: dict[str, dict[str, Any]] = {}
    paths: dict[str, dict[str, dict[str, str]]] = {}
    for route, path_item in document.get("paths", {}).items():
        route_operations: dict[str, dict[str, str]] = {}
        for method, operation in path_item.items():
            if method.lower() not in HTTP_METHODS or not isinstance(operation, dict):
                continue
            operation_id = operation.get("operationId") or f"{method}_{route}"
            operation_name = _identifier(operation_id)
            if operation_name in operations:
                raise ValueError(f"Duplicate OpenAPI operationId: {operation_id}")
            operations[operation_name] = operation
            route_operations[method.lower()] = {"operation": operation_name}
        if route_operations:
            paths[route] = route_operations

    lines = [
        "/**",
        " * Generated from the live FastAPI OpenAPI schema. Do not edit by hand.",
        " * Regenerate with `npm run api:types` and check with `npm run api:types:check`.",
        " */",
        "export interface components {",
        "  schemas: {",
    ]
    for name, schema in schemas.items():
        lines.append(f"    {_key(name)}: {schema_type(schema)};")
    lines.extend(["  };", "}", "", "export interface operations {"])
    for name, operation in operations.items():
        lines.append(f"  {_key(name)}: {render_operation(operation)};")
    lines.extend(["}", "", "export interface paths {"])
    for route, route_operations in paths.items():
        lines.append(f"  {_key(route)}: {{")
        for method, reference in route_operations.items():
            lines.append(f"    {method}: operations[{_key(reference['operation'])}];")
        lines.append("  };")
    lines.extend(["}", ""])
    return "\n".join(lines)


def load_openapi() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from web.api.app import app  # Import only when generating; tests can exercise renderers without runtime deps.

    return app.openapi()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail when committed generated types differ from OpenAPI.")
    args = parser.parse_args()
    rendered = render_openapi_types(load_openapi())
    if args.check:
        try:
            existing = OUTPUT.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        if existing != rendered:
            print(f"Generated API types are stale: run `npm run api:types` to update {OUTPUT}", file=sys.stderr)
            return 1
        print(f"OpenAPI types are current: {OUTPUT.relative_to(ROOT)}")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8")
    print(f"Generated {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
