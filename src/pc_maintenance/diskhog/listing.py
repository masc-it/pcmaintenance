"""Directory listing backends that return allocation metadata per entry.

On macOS, ``getattrlistbulk(2)`` returns the attributes of many entries in
one system call and releases the GIL while the kernel fills the buffer. That
is markedly faster than ``scandir`` plus one ``lstat`` per entry, which is
what the portable backend does.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat as stat_mod
import struct
import sys
import threading
from collections.abc import Callable

REGULAR = 1
DIRECTORY = 2
OTHER = 0

# (name, kind, st_dev, inode, link count, allocated bytes, mtime seconds)
Entry = tuple[str, int, int, int, int, int, int]
# Entries plus the number of entries whose metadata could not be read.
Listing = tuple[list[Entry], int]
ListDir = Callable[[str], Listing]


def list_dir_portable(path: str) -> Listing:
    entries: list[Entry] = []
    failures = 0
    with os.scandir(path) as it:
        for entry in it:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                failures += 1
                continue
            mode = st.st_mode
            if stat_mod.S_ISDIR(mode):
                kind = DIRECTORY
            elif stat_mod.S_ISREG(mode):
                kind = REGULAR
            else:
                kind = OTHER
            entries.append(
                (
                    entry.name,
                    kind,
                    st.st_dev,
                    st.st_ino,
                    st.st_nlink,
                    st.st_blocks * 512,
                    int(st.st_mtime),
                )
            )
    return entries, failures


class _AttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_uint16),
        ("commonattr", ctypes.c_uint32),
        ("volattr", ctypes.c_uint32),
        ("dirattr", ctypes.c_uint32),
        ("fileattr", ctypes.c_uint32),
        ("forkattr", ctypes.c_uint32),
    ]


_ATTR_CMN_NAME = 0x00000001
_ATTR_CMN_DEVID = 0x00000002
_ATTR_CMN_OBJTYPE = 0x00000008
_ATTR_CMN_MODTIME = 0x00000400
_ATTR_CMN_FILEID = 0x02000000
_ATTR_CMN_ERROR = 0x20000000
_ATTR_CMN_RETURNED_ATTRS = 0x80000000
_ATTR_FILE_LINKCOUNT = 0x00000001
_ATTR_FILE_ALLOCSIZE = 0x00000004
_FSOPT_PACK_INVAL_ATTRS = 0x00000008  # fixed layout: invalid attrs are zeroed
_VREG = 1
_VDIR = 2

# Packed in kernel order, 4-byte aligned: length, returned attribute set,
# error, name reference, devid, objtype, mtime, fileid, linkcount, allocsize.
_RECORD = struct.Struct("=I20xIiIiIqqQIq")
# Directories carry no file attributes, so their records end before the
# linkcount/allocsize fields; those two values are ignored for directories.
_NAME_BASE = 4 + 20 + 4  # the name reference is relative to its own offset
_BUFFER_SIZE = 128 * 1024
_BUFFER_SLACK = _RECORD.size  # fixed-size unpack of a short final record


class _BulkLister:
    def __init__(self) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        self._call = libc.getattrlistbulk
        self._call.argtypes = [
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint64,
        ]
        self._call.restype = ctypes.c_int
        self._attrs = _AttrList(
            5,
            0,
            _ATTR_CMN_RETURNED_ATTRS
            | _ATTR_CMN_ERROR
            | _ATTR_CMN_NAME
            | _ATTR_CMN_DEVID
            | _ATTR_CMN_OBJTYPE
            | _ATTR_CMN_MODTIME
            | _ATTR_CMN_FILEID,
            0,
            0,
            _ATTR_FILE_LINKCOUNT | _ATTR_FILE_ALLOCSIZE,
            0,
        )
        self._local = threading.local()

    def __call__(self, path: str) -> Listing:
        buf = getattr(self._local, "buf", None)
        if buf is None:
            buf = self._local.buf = ctypes.create_string_buffer(
                _BUFFER_SIZE + _BUFFER_SLACK
            )
        view = memoryview(buf).cast("B")
        unpack = _RECORD.unpack_from
        entries: list[Entry] = []
        failures = 0
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            while True:
                count = self._call(
                    fd,
                    ctypes.byref(self._attrs),
                    buf,
                    _BUFFER_SIZE,
                    _FSOPT_PACK_INVAL_ATTRS,
                )
                if count < 0:
                    code = ctypes.get_errno()
                    if code in (errno.ENOTSUP, errno.EINVAL):
                        return list_dir_portable(path)  # filesystem lacks support
                    raise OSError(code, os.strerror(code), path)
                if count == 0:
                    return entries, failures
                offset = 0
                for _ in range(count):
                    (
                        length,
                        error,
                        name_offset,
                        name_length,
                        dev,
                        objtype,
                        mtime,
                        _nsec,
                        inode,
                        links,
                        allocated,
                    ) = unpack(view, offset)
                    if error:
                        failures += 1
                    else:
                        start = offset + _NAME_BASE + name_offset
                        name = bytes(view[start : start + name_length - 1]).decode(
                            "utf-8", "surrogateescape"
                        )
                        if objtype == _VDIR:
                            kind = DIRECTORY
                            links = allocated = 0
                        elif objtype == _VREG:
                            kind = REGULAR
                        else:
                            kind = OTHER
                        entries.append(
                            (name, kind, dev, inode, links, allocated, mtime)
                        )
                    offset += length
        finally:
            os.close(fd)


def _default_list_dir() -> ListDir:
    if sys.platform == "darwin":
        try:
            return _BulkLister()
        except (OSError, AttributeError):
            pass  # older libc without getattrlistbulk
    return list_dir_portable


list_dir: ListDir = _default_list_dir()
