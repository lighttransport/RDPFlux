from __future__ import annotations

import json
from typing import Any

from . import execute
from .actions import ActionError


def _name(value: Any, label: str = "name") -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ActionError(f"{label} must be a non-empty string of at most 256 characters")
    return value.strip()


def _pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 2**31 - 1:
        raise ActionError("pid must be an integer between 1 and 2147483647")
    return value


async def _powershell(script: str, timeout: float = 30.0) -> dict[str, Any]:
    result = await execute.run({
        "command": ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        "timeout": timeout,
    })
    if result["exit_code"] != 0:
        message = result["stderr"].strip() or result["stdout"].strip() or "PowerShell operation failed"
        raise ActionError(message[:1024])
    return result


async def process_list() -> dict[str, Any]:
    result = await _powershell(
        "Get-Process | Select-Object Id,ProcessName,CPU,WorkingSet64 | ConvertTo-Json -Compress")
    return {"processes": _json_array(result["stdout"])}


async def process_terminate(pid: Any) -> dict[str, Any]:
    process_id = _pid(pid)
    await _powershell(f"Stop-Process -Id {process_id} -Force")
    return {"pid": process_id, "terminated": True}


async def service_list(name: Any = None) -> dict[str, Any]:
    selector = ""
    if name is not None:
        service_name = _name(name)
        selector = f" -Name '{service_name.replace(chr(39), chr(39) * 2)}'"
    result = await _powershell(
        f"Get-Service{selector} | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Compress")
    return {"services": _json_array(result["stdout"])}


async def service_control(action: Any, name: Any) -> dict[str, Any]:
    operation = _name(action, "action").lower()
    if operation not in ("start", "stop", "restart"):
        raise ActionError("action must be start, stop, or restart")
    service_name = _name(name)
    quoted = service_name.replace(chr(39), chr(39) * 2)
    command = (f"Restart-Service -Name '{quoted}' -ErrorAction Stop" if operation == "restart"
               else f"{operation.title()}-Service -Name '{quoted}' -ErrorAction Stop")
    await _powershell(command)
    return {"name": service_name, "action": operation, "ok": True}


async def task_list(name: Any = None) -> dict[str, Any]:
    selector = ""
    if name is not None:
        task_name = _name(name)
        selector = f" -TaskName '{task_name.replace(chr(39), chr(39) * 2)}'"
    result = await _powershell(
        f"Get-ScheduledTask{selector} | Select-Object TaskName,TaskPath,State | ConvertTo-Json -Compress")
    return {"tasks": _json_array(result["stdout"])}


async def task_run(name: Any) -> dict[str, Any]:
    task_name = _name(name)
    quoted = task_name.replace(chr(39), chr(39) * 2)
    await _powershell(f"Start-ScheduledTask -TaskName '{quoted}'")
    return {"name": task_name, "started": True}


async def diagnostics() -> dict[str, Any]:
    result = await _powershell(
        "[pscustomobject]@{computer=$env:COMPUTERNAME; user=$env:USERNAME; "
        "os=(Get-CimInstance Win32_OperatingSystem).Caption; "
        "version=(Get-CimInstance Win32_OperatingSystem).Version; "
        "uptime=((Get-Date)-(Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalSeconds} "
        "| ConvertTo-Json -Compress")
    try:
        value = json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise ActionError("diagnostics returned invalid JSON") from exc
    return value if isinstance(value, dict) else {"value": value}


def _json_array(text: str) -> list[Any]:
    try:
        value = json.loads(text) if text.strip() else []
    except json.JSONDecodeError as exc:
        raise ActionError("PowerShell returned invalid JSON") from exc
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
