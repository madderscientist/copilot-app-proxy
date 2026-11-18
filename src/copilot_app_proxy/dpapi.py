"""Windows DPAPI (CurrentUser scope) encryption helpers using crypt32.dll."""

from __future__ import annotations

import base64
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiError(RuntimeError):
    """Raised when Windows DPAPI protection or unprotection fails."""


if sys.platform == "win32":

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _ensure_windows() -> None:
    if sys.platform != "win32":
        raise DpapiError("DPAPI is only supported on Windows.")


def protect_bytes(plain: bytes) -> bytes:
    """Encrypt bytes using CurrentUser DPAPI."""
    _ensure_windows()
    if not plain:
        raise DpapiError("DPAPI protect failed: input is empty.")
    buf = (ctypes.c_ubyte * len(plain)).from_buffer_copy(plain)
    blob_in = _DataBlob(len(plain), buf)
    blob_out = _DataBlob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    ):
        raise DpapiError("DPAPI protect failed.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def unprotect_bytes(cipher: bytes) -> bytes:
    """Decrypt CurrentUser DPAPI-protected bytes."""
    _ensure_windows()
    if not cipher:
        raise DpapiError("DPAPI unprotect failed: input is empty.")
    buf = (ctypes.c_ubyte * len(cipher)).from_buffer_copy(cipher)
    blob_in = _DataBlob(len(cipher), buf)
    blob_out = _DataBlob()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    ):
        raise DpapiError("DPAPI unprotect failed.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        _kernel32.LocalFree(blob_out.pbData)


def protect_text(plain_text: str) -> str:
    """Encrypt UTF-8 text and return a base64-encoded DPAPI payload."""
    if not plain_text.strip():
        raise DpapiError("DPAPI protect failed: input is empty.")
    encrypted = protect_bytes(plain_text.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def unprotect_text(encoded_text: str) -> str:
    """Decode base64 DPAPI payload and return UTF-8 text."""
    stripped = encoded_text.strip()
    if not stripped:
        raise DpapiError("DPAPI unprotect failed: input is empty.")
    try:
        cipher = base64.b64decode(stripped, validate=True)
    except Exception as exc:
        raise DpapiError("DPAPI unprotect failed: invalid base64 payload.") from exc
    return unprotect_bytes(cipher).decode("utf-8")


def save_protected_text(file_path: str | Path, plain_text: str) -> None:
    """Atomically write DPAPI-protected base64 text to disk."""
    target = Path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encrypted = protect_text(plain_text)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".dpapi", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encrypted)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_protected_text(file_path: str | Path) -> str:
    """Read and decrypt DPAPI-protected base64 text from disk."""
    encoded = Path(file_path).read_text(encoding="utf-8")
    return unprotect_text(encoded)
