"""Windows DPAPI (CurrentUser scope) encryption helpers using crypt32.dll."""

from __future__ import annotations

import base64
import binascii
import ctypes
import os
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

__all__ = [
    "DpapiError",
    "load_protected_text",
    "protect_text",
    "save_protected_text",
    "unprotect_text",
]

_CRYPTPROTECT_UI_FORBIDDEN = 0x01


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


def _crypt_bytes(data: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise DpapiError("DPAPI is only supported on Windows.")

    operation = "unprotect" if decrypt else "protect"
    if not data:
        raise DpapiError(f"DPAPI {operation} failed: input is empty.")
    transform = _crypt32.CryptUnprotectData if decrypt else _crypt32.CryptProtectData
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    blob_in = _DataBlob(len(data), buf)
    blob_out = _DataBlob()
    if not transform(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    ):
        raise DpapiError(f"DPAPI {operation} failed.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        # DPAPI allocates the output buffer; Python does not own this memory.
        _kernel32.LocalFree(blob_out.pbData)


def protect_text(plain_text: str) -> str:
    """Encrypt UTF-8 text and return a base64-encoded DPAPI payload."""
    if not plain_text.strip():
        raise DpapiError("DPAPI protect failed: input is empty.")
    encrypted = _crypt_bytes(plain_text.encode("utf-8"), decrypt=False)
    return base64.b64encode(encrypted).decode("ascii")


def unprotect_text(encoded_text: str) -> str:
    """Decode base64 DPAPI payload and return UTF-8 text."""
    stripped = encoded_text.strip()
    if not stripped:
        raise DpapiError("DPAPI unprotect failed: input is empty.")
    try:
        cipher = base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DpapiError("DPAPI unprotect failed: invalid base64 payload.") from exc
    return _crypt_bytes(cipher, decrypt=True).decode("utf-8")


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
    """Read and decrypt DPAPI-protected base64 text from disk (tolerates UTF-8 BOM)."""
    encoded = Path(file_path).read_text(encoding="utf-8-sig")
    return unprotect_text(encoded)
