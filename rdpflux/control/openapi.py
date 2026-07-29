from __future__ import annotations

from typing import Any

from .actions import ACTIONS, MAX_SCROLL_AMOUNT, SCROLL_DIRECTIONS

# One OpenAPI document generated from the shared action table, so the schema an
# OpenAI-style function caller sees never drifts from what the agent enforces.

_COORDINATE = {
    "type": "array", "items": {"type": "integer", "minimum": 0},
    "minItems": 2, "maxItems": 2,
    "description": "Point in delivered-screenshot pixel space, [x, y].",
}


def _action_schema() -> dict[str, Any]:
    variants = []
    for spec in ACTIONS.values():
        properties: dict[str, Any] = {"action": {"const": spec.name, "description": spec.description}}
        for field in spec.accepted:
            properties[field] = _field_schema(field)
        variants.append({
            "type": "object",
            "properties": properties,
            "required": ["action", *spec.required],
            "additionalProperties": False,
        })
    return {"oneOf": variants}


def _field_schema(field: str) -> dict[str, Any]:
    if field in ("coordinate", "start_coordinate"):
        return _COORDINATE
    if field == "text":
        return {"type": "string", "description": "Literal text, or a key/chord like 'ctrl+s'."}
    if field == "duration":
        return {"type": "number", "minimum": 0, "description": "Seconds."}
    if field == "scroll_amount":
        return {"type": "integer", "minimum": 1, "maximum": MAX_SCROLL_AMOUNT}
    if field == "scroll_direction":
        return {"type": "string", "enum": list(SCROLL_DIRECTIONS)}
    return {}


def build_spec(*, exec_enabled: bool, files_enabled: bool) -> dict[str, Any]:
    paths: dict[str, Any] = {
        "/v1/screenshot": {
            "post": {
                "operationId": "screenshot",
                "summary": "Capture the remote desktop.",
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "width": {"type": "integer", "minimum": 320, "maximum": 2576,
                                   "description": "Delivered image width; the height follows the display aspect ratio."},
                        "format": {"type": "string", "enum": ["png", "jpeg"], "default": "png"},
                        "quality": {"type": "integer", "minimum": 1, "maximum": 100, "default": 80},
                    },
                }}}},
                "responses": {"200": {"description": "PNG or JPEG image.",
                                       "content": {"image/png": {}, "image/jpeg": {}}}},
            }
        },
        "/v1/action": {
            "post": {
                "operationId": "action",
                "summary": "Perform a mouse or keyboard action.",
                "requestBody": {"required": True,
                                 "content": {"application/json": {"schema": _action_schema()}}},
                "responses": {"200": {"description": "Action result.",
                                       "content": {"application/json": {}}}},
            }
        },
    }
    if exec_enabled:
        paths["/v1/exec"] = {"post": {
            "operationId": "exec",
            "summary": "Run a command on the remote host.",
            "requestBody": {"required": True, "content": {"application/json": {"schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                 "description": "argv list; not passed through a shell."},
                    "timeout": {"type": "number", "minimum": 0, "maximum": 300, "default": 30},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            }}}},
            "responses": {"200": {"description": "exit_code, stdout, stderr.",
                                   "content": {"application/json": {}}}},
        }}
    if files_enabled:
        paths["/v1/file"] = {
            "get": {"operationId": "readFile", "summary": "Read a file from the remote host.",
                     "parameters": [{"name": "path", "in": "query", "required": True,
                                     "schema": {"type": "string"}}],
                     "responses": {"200": {"description": "File bytes.",
                                            "content": {"application/octet-stream": {}}}}},
            "put": {"operationId": "writeFile", "summary": "Write a file on the remote host.",
                     "parameters": [
                         {"name": "path", "in": "query", "required": True, "schema": {"type": "string"}},
                         {"name": "create_parents", "in": "query", "schema": {"type": "boolean"}},
                     ],
                     "requestBody": {"required": True,
                                      "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                     "responses": {"200": {"description": "Write result.",
                                            "content": {"application/json": {}}}}},
        }
        paths["/v1/dir"] = {"get": {"operationId": "listDir", "summary": "List a directory.",
                                     "parameters": [{"name": "path", "in": "query",
                                                     "schema": {"type": "string"}}],
                                     "responses": {"200": {"description": "Directory entries.",
                                                           "content": {"application/json": {}}}}}}

    return {
        "openapi": "3.1.0",
        "info": {"title": "rdpflux desktop control", "version": "1"},
        "paths": paths,
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
    }
