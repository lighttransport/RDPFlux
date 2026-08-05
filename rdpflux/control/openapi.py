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


def build_spec(*, exec_enabled: bool, files_enabled: bool,
               system_enabled: bool = False, clipboard_enabled: bool = False) -> dict[str, Any]:
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
        paths["/v1/sessions"] = {"post": {
            "operationId": "openShellSession",
            "summary": "Open a persistent PowerShell or Bash session.",
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {"program": {"type": "string", "default": "powershell"},
                               "cwd": {"type": "string"}},
            }}}},
            "responses": {"200": {"description": "Session identifier.",
                                   "content": {"application/json": {}}}},
        }}
        paths["/v1/sessions/{session}/run"] = {"post": {
            "operationId": "runShellSessionCommand",
            "summary": "Run a command in a persistent shell session.",
            "parameters": [{"name": "session", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "requestBody": {"required": True, "content": {"application/json": {
                "schema": {"type": "object", "required": ["command"],
                           "properties": {"command": {"type": "string"}}}}}},
            "responses": {"200": {"description": "Command output and exit code.",
                                   "content": {"application/json": {}}}},
        }}
        paths["/v1/sessions/{session}/interrupt"] = {"post": {
            "operationId": "interruptShellSession",
            "summary": "Interrupt the active session command.",
            "parameters": [{"name": "session", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Interrupt result.",
                                   "content": {"application/json": {}}}},
        }}
        paths["/v1/sessions/{session}/close"] = {"delete": {
            "operationId": "closeShellSession",
            "summary": "Close a persistent shell session.",
            "parameters": [{"name": "session", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "Close result.",
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

    if system_enabled:
        paths.update({
            "/v1/system/processes": {
                "get": {"operationId": "listProcesses", "summary": "List processes.",
                         "responses": {"200": {"description": "Process list.",
                                                "content": {"application/json": {}}}}}},
            "/v1/system/processes/terminate": {
                "post": {"operationId": "terminateProcess", "summary": "Terminate a process.",
                          "requestBody": {"required": True, "content": {"application/json": {
                              "schema": {"type": "object", "required": ["pid"],
                                         "properties": {"pid": {"type": "integer", "minimum": 1}}}}}},
                          "responses": {"200": {"description": "Termination result.",
                                                 "content": {"application/json": {}}}}}},
            "/v1/system/services": {
                "get": {"operationId": "listServices", "summary": "List Windows services.",
                         "parameters": [{"name": "name", "in": "query",
                                         "schema": {"type": "string"}}],
                         "responses": {"200": {"description": "Service list.",
                                                "content": {"application/json": {}}}}}},
            "/v1/system/services/control": {
                "post": {"operationId": "controlService", "summary": "Start, stop, or restart a service.",
                          "requestBody": {"required": True, "content": {"application/json": {
                              "schema": {"type": "object", "required": ["action", "name"],
                                         "properties": {"action": {"type": "string", "enum": ["start", "stop", "restart"]},
                                                        "name": {"type": "string"}}}}}},
                          "responses": {"200": {"description": "Service operation result.",
                                                 "content": {"application/json": {}}}}}},
            "/v1/system/tasks": {
                "get": {"operationId": "listTasks", "summary": "List scheduled tasks.",
                         "parameters": [{"name": "name", "in": "query",
                                         "schema": {"type": "string"}}],
                         "responses": {"200": {"description": "Task list.",
                                                "content": {"application/json": {}}}}}},
            "/v1/system/tasks/run": {
                "post": {"operationId": "runTask", "summary": "Run a scheduled task.",
                          "requestBody": {"required": True, "content": {"application/json": {
                              "schema": {"type": "object", "required": ["name"],
                                         "properties": {"name": {"type": "string"}}}}}},
                          "responses": {"200": {"description": "Task operation result.",
                                                 "content": {"application/json": {}}}}}},
            "/v1/system/diagnostics": {
                "get": {"operationId": "diagnostics", "summary": "Get host diagnostics.",
                         "responses": {"200": {"description": "Host diagnostics.",
                                                "content": {"application/json": {}}}}}},
        })

    if clipboard_enabled:
        paths["/v1/clipboard"] = {
            "get": {"operationId": "readClipboard", "summary": "Read remote clipboard text.",
                     "responses": {"200": {"description": "Clipboard text.",
                                            "content": {"application/json": {}}}}},
            "put": {"operationId": "writeClipboard", "summary": "Write remote clipboard text.",
                     "requestBody": {"required": True, "content": {"application/json": {
                         "schema": {"type": "object", "required": ["text"],
                                    "properties": {"text": {"type": "string"}}}}}},
                     "responses": {"200": {"description": "Clipboard result.",
                                            "content": {"application/json": {}}}}},
        }

    return {
        "openapi": "3.1.0",
        "info": {"title": "rdpflux desktop control", "version": "1"},
        "paths": paths,
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
    }
