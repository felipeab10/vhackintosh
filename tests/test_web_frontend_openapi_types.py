"""Regression coverage for the small OpenAPI-to-TypeScript renderer."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web/frontend/scripts"))
from generate_api_types import render_openapi_types, schema_type  # noqa: E402


class OpenApiTypeRendererTests(unittest.TestCase):
    def test_schema_renderer_handles_refs_enums_unions_and_nested_objects(self):
        schema = {
            "type": "object",
            "required": ["id", "state"],
            "properties": {
                "id": {"type": "string"},
                "state": {"enum": ["ready", "stopped"]},
                "detail": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "items": {"type": "array", "items": {"$ref": "#/components/schemas/Item"}},
            },
        }
        rendered = schema_type(schema)
        self.assertIn('"id": string;', rendered)
        self.assertIn('"state": "ready" | "stopped";', rendered)
        self.assertIn('"detail"?: (string) | (null);', rendered)
        self.assertIn('Array<components["schemas"]["Item"]>', rendered)

    def test_openapi_renderer_emits_paths_operations_and_component_schemas(self):
        document = {
            "components": {"schemas": {"Body": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            "paths": {
                "/api/v1/vms": {
                    "post": {
                        "operationId": "create_vm",
                        "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}},
                        "responses": {"201": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}}},
                    }
                }
            },
        }
        rendered = render_openapi_types(document)
        self.assertIn('"Body": {', rendered)
        self.assertIn('"create_vm": {', rendered)
        self.assertIn('"/api/v1/vms": {', rendered)
        self.assertIn('post: operations["create_vm"];', rendered)
        self.assertIn('requestBody: components["schemas"]["Body"]', rendered)

    def test_generation_is_deterministic_for_same_schema(self):
        document = {"components": {"schemas": {"B": {"type": "boolean"}, "A": {"type": "string"}}}, "paths": {}}
        self.assertEqual(render_openapi_types(document), render_openapi_types(document))


if __name__ == "__main__":
    unittest.main()
