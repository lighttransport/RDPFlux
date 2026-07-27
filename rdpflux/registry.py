from __future__ import annotations

import os
import sys

PLUGIN_CLSID = "{2E719E6B-495C-4A9D-93A8-8A254F735D41}"
ADDIN_NAME = "RDPFluxPython"


def _require_windows():
    if os.name != "nt":
        raise RuntimeError("mstsc plugin registration is only supported on Windows")
    import winreg
    return winreg


def _paths() -> tuple[str, str, str]:
    clsid = rf"Software\Classes\CLSID\{PLUGIN_CLSID}"
    return clsid, rf"{clsid}\LocalServer32", rf"Software\Microsoft\Terminal Server Client\Default\AddIns\{ADDIN_NAME}"


def _server_command() -> str:
    executable = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return f'"{executable}" -Embedding'
    return f'"{executable}" -m rdpflux.client -Embedding'


def register(*, machine: bool = False) -> None:
    winreg = _require_windows()
    root = winreg.HKEY_LOCAL_MACHINE if machine else winreg.HKEY_CURRENT_USER
    clsid, server, addin = _paths()
    with winreg.CreateKeyEx(root, clsid, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Python TCP over RDP")
    with winreg.CreateKeyEx(root, server, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, _server_command())
    with winreg.CreateKeyEx(root, addin, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Name", 0, winreg.REG_SZ, PLUGIN_CLSID)


def _delete_tree(winreg, root, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(winreg, root, path + "\\" + child)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def unregister(*, machine: bool = False) -> None:
    winreg = _require_windows()
    root = winreg.HKEY_LOCAL_MACHINE if machine else winreg.HKEY_CURRENT_USER
    clsid, _server, addin = _paths()
    _delete_tree(winreg, root, addin)
    _delete_tree(winreg, root, clsid)
