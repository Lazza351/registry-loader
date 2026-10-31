"""Linux ingress close tracking and conservative, three-valued open-writer probes."""

import ctypes
import errno
import fcntl
import logging
import os
import re
import signal
import struct
import time
from pathlib import Path

log = logging.getLogger(__name__)
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_TO = 0x00000080
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
EVENT = struct.Struct("iIII")
REMOTE = {"nfs", "nfs4", "cifs", "smb3", "9p", "fuse"}
F_SETLEASE = 1024
# Close marks only matter until the entry is claimed; expired ones must not accumulate
# for ignored names (.part files, unsupported suffixes) that are never processed.
CLOSED_TTL = 900.0
_lease_signals_ready = False


def filesystem_type(directory, mountinfo="/proc/self/mountinfo"):
    """Use the longest mount-point match, including bind mounts and escaped names."""
    directory = Path(directory).resolve()
    matches = []
    try:
        with open(mountinfo) as stream:
            for line in stream:
                before, separator, after = line.partition(" - ")
                if not separator:
                    continue
                fields = before.split()
                mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
                if mount == directory or mount in directory.parents:
                    matches.append((len(mount.parts), after.split()[0]))
    except OSError:
        return None
    return max(matches, default=(0, None), key=lambda item: item[0])[1]


class IngressGuard:
    def __init__(self, directories):
        self.fd = -1
        self.watches = {}
        self.done = {}
        self.degraded = False
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if self.fd < 0:
                return
            for directory in directories:
                kind = filesystem_type(directory)
                if kind is None or kind in REMOTE or kind.startswith("fuse."):
                    continue
                wd = libc.inotify_add_watch(
                    self.fd, os.fsencode(directory), IN_CLOSE_WRITE | IN_MOVED_TO
                )
                if wd >= 0:
                    self.watches[wd] = Path(directory)
        except (OSError, AttributeError):
            self.close()

    def supported(self, directory):
        return not self.degraded and Path(directory) in self.watches.values()

    def poll(self, now=None):
        now = time.monotonic() if now is None else now
        self.done = {key: seen for key, seen in self.done.items() if now - seen <= CLOSED_TTL}
        if self.fd < 0:
            return
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            except InterruptedError:
                continue
            except OSError:
                self._degrade()
                return
            if not data:
                return
            self._events(data)

    def _degrade(self):
        if not self.degraded:
            log.warning("Ingress close tracking degraded; falling back to stability checks")
        self.degraded = True
        self.done.clear()

    def _events(self, data):
        offset = 0
        while offset + EVENT.size <= len(data):
            wd, mask, _cookie, length = EVENT.unpack_from(data, offset)
            offset += EVENT.size
            name = data[offset : offset + length].split(b"\0", 1)[0]
            offset += length
            if mask & IN_Q_OVERFLOW:
                self._degrade()
            if mask & IN_IGNORED:
                self.watches.pop(wd, None)
            directory = self.watches.get(wd)
            if self.degraded or directory is None or not name:
                continue
            if mask & (IN_CLOSE_WRITE | IN_MOVED_TO):
                try:
                    info = os.stat(directory / os.fsdecode(name), follow_symlinks=False)
                except OSError:
                    continue
                self.done[(info.st_dev, info.st_ino)] = time.monotonic()

    def completed(self, path):
        if not self.supported(Path(path).parent):
            return False
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) in self.done

    def forget(self, identity):
        self.done.pop(identity, None)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        self.watches.clear()
        self.done.clear()


def proc_writer_open(path, proc=Path("/proc")):
    """An inaccessible process may own the writer; never turn that into a false negative."""
    try:
        target = os.stat(path, follow_symlinks=False)
        processes = list(proc.iterdir())
    except OSError:
        return None
    unknown = False
    for process in processes:
        if not process.name.isdigit():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
            for descriptor in descriptors:
                try:
                    info = descriptor.stat()
                    if (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
                        continue
                    text = (process / "fdinfo" / descriptor.name).read_text()
                    flags = next(line.split()[1] for line in text.splitlines()
                                 if line.startswith("flags:"))
                    if int(flags, 8) & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR}:
                        return True
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, StopIteration):
                    unknown = True
        except FileNotFoundError:
            continue
        except OSError:
            unknown = True
    return None if unknown else False


def writer_open(path):
    """A write lease proves exclusivity; denied/unsupported leases use /proc instead."""
    global _lease_signals_ready
    try:
        if not _lease_signals_ready:
            # A lease break can queue SIGIO even in the tiny acquire/release window.
            signal.signal(signal.SIGIO, signal.SIG_IGN)
            _lease_signals_ready = True
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except (OSError, ValueError):
        return proc_writer_open(path)
    leased = False
    try:
        try:
            fcntl.fcntl(fd, F_SETLEASE, fcntl.F_WRLCK)
            leased = True
            return False
        except OSError as error:
            if error.errno == errno.EAGAIN:
                return True
    finally:
        try:
            if leased:
                fcntl.fcntl(fd, F_SETLEASE, fcntl.F_UNLCK)
        finally:
            os.close(fd)
    return proc_writer_open(path)
