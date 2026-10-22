"""Recycle a generated recovery bundle with the Windows Shell, never hard-delete."""
import ctypes
import os
import uuid
from pathlib import Path


def recycle_file(path):
    path = Path(path).resolve(strict=True)
    if os.name != 'nt':
        raise OSError('此系统尚不支持安全转入电脑回收站；内容将继续保留')
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_ubyte * 16)]
        def __init__(self, value):
            super().__init__((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))

    ole = ctypes.OleDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    ptr = ctypes.c_void_p
    hr = ctypes.c_long
    ole.CoInitializeEx.argtypes = [ptr, wintypes.DWORD]
    ole.CoInitializeEx.restype = hr
    ole.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ptr, wintypes.DWORD, ctypes.POINTER(GUID), ctypes.POINTER(ptr)]
    ole.CoCreateInstance.restype = hr
    shell.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ptr, ctypes.POINTER(GUID), ctypes.POINTER(ptr)]
    shell.SHCreateItemFromParsingName.restype = hr
    shell.SHQueryRecycleBinW.argtypes = [wintypes.LPCWSTR, ptr]
    shell.SHQueryRecycleBinW.restype = hr

    def check(result):
        if result < 0:
            raise OSError(f'电脑回收站操作失败（{result & 0xffffffff:08X}），内容仍保留')

    def call(obj, index, types=(), args=()):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ptr))).contents
        return ctypes.WINFUNCTYPE(hr, ptr, *types)(table[index])(obj, *args)

    # Refuse remote/unsupported volumes. There is deliberately no permanent-delete fallback.
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    if kernel.GetDriveTypeW(path.anchor) != 3:
        raise OSError('恢复包必须位于支持回收站的本机固定磁盘；内容仍保留')
    class BinInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('bytes', ctypes.c_longlong), ('items', ctypes.c_longlong)]
    info = BinInfo(); info.size = ctypes.sizeof(info)
    check(shell.SHQueryRecycleBinW(path.anchor, ctypes.byref(info)))
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and initialized != -2147417850:
        check(initialized)
    operation, item = ptr(), ptr()
    try:
        check(ole.CoCreateInstance(ctypes.byref(GUID('3ad05575-8857-4850-9277-11b85bdb8e09')), None, 1,
                                  ctypes.byref(GUID('947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8')), ctypes.byref(operation)))
        check(shell.SHCreateItemFromParsingName(str(path), None,
                    ctypes.byref(GUID('43826d1e-e718-42ee-bc55-a1e261c37bfe')), ctypes.byref(item)))
        # FOFX_RECYCLEONDELETE | EARLYFAILURE | ADDUNDORECORD | ALLOWUNDO | NO_UI.
        check(call(operation, 5, (wintypes.DWORD,), (0x20180040 | 0x614,)))
        check(call(operation, 18, (ptr, ptr), (item, None)))
        check(call(operation, 21))
        aborted = wintypes.BOOL()
        check(call(operation, 22, (ctypes.POINTER(wintypes.BOOL),), (ctypes.byref(aborted),)))
        if aborted.value or path.exists():
            raise OSError('电脑回收站未完成接收；内容仍保留')
    finally:
        if item: call(item, 2)
        if operation: call(operation, 2)
        if initialized >= 0: ole.CoUninitialize()
