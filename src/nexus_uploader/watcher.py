"""Polling queue with durable results and at-least-once crash recovery (Linux/POSIX)."""

import errno
import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import time
import uuid

from .archives import classify
from .upload import UploadError

log = logging.getLogger(__name__)


class ClaimChanged(OSError):
    """An ingress entry changed during claim; never upload the replacement."""


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identical_job(source, target):
    if target.is_symlink() or not target.is_dir():
        return False
    left, right = {p.name for p in source.iterdir()}, {p.name for p in target.iterdir()}
    if left != right:
        return False
    for name in left:
        a, b = source / name, target / name
        if a.is_symlink() or b.is_symlink():
            if not (a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)):
                return False
        elif stat.S_ISREG(a.lstat().st_mode) and stat.S_ISREG(b.lstat().st_mode):
            with a.open("rb") as first, b.open("rb") as second:
                if (
                    hashlib.file_digest(first, "sha256").digest()
                    != hashlib.file_digest(second, "sha256").digest()
                ):
                    return False
        else:
            return False
    return True


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    sync_directory(path.parent)


def signature(path):
    s = path.lstat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def move_file(source, target, expected=None):
    """Unique destination only. Cross-filesystem copy is fsynced before source removal."""
    before = signature(source)
    if not stat.S_ISREG(source.lstat().st_mode) or (expected is not None and before != expected):
        raise ClaimChanged("Ingress entry changed before claim")
    try:
        os.rename(source, target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        temporary = target.with_name(".claim-part")
        try:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as src, temporary.open("xb") as dst:
                info = os.fstat(src.fileno())
                opened = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                if opened != before or not stat.S_ISREG(info.st_mode):
                    raise ClaimChanged("Ingress entry changed while opening")
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            if signature(source) != before:
                raise ClaimChanged("Producer modified source during cross-filesystem claim")
            os.replace(temporary, target)
            sync_directory(target.parent)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        source.unlink()
    else:
        if signature(target) != before or not stat.S_ISREG(target.lstat().st_mode):
            raise ClaimChanged("Ingress entry changed during rename")
        sync_directory(target.parent)
    sync_directory(source.parent)


class Watcher:
    def __init__(self, config, uploader):
        self.c, self.uploader = config, uploader
        self.seen = {}
        self.locks = []

    def __enter__(self):
        for directory in (
            *self.c.watch_dirs,
            self.c.processing_dir,
            self.c.success_dir,
            self.c.error_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            # Lock both ingress and shared processing so differently configured
            # workers cannot claim or recover the same job concurrently.
            for directory in sorted({*self.c.watch_dirs, self.c.processing_dir}):
                lock = (directory / ".nexus-uploader.lock").open("a")
                self.locks.append(lock)
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            for lock in self.locks:
                lock.close()
            self.locks.clear()
            raise RuntimeError("Uploader already running for this input directory") from None
        return self

    def __exit__(self, *args):
        for lock in self.locks:
            lock.close()
        self.locks.clear()

    def scan(self, now=None, stop=None):
        now = time.monotonic() if now is None else now
        # Complete/recover previously claimed files first; completion markers prevent reupload.
        for job in sorted(self.c.processing_dir.glob("job-*")):
            if stop is not None and stop.is_set():
                return
            if job.is_dir() and not job.is_symlink():
                self._process(job)
        current = set()
        for path in sorted(p for directory in self.c.watch_dirs for p in directory.iterdir()):
            if stop is not None and stop.is_set():
                break
            try:
                if not stat.S_ISREG(path.lstat().st_mode) or not classify(path):
                    continue
                current.add(path)
                sig = signature(path)
                previous = self.seen.get(path)
                if previous is None or previous[0] != sig:
                    self.seen[path] = (sig, now)
                    continue
                if now - previous[1] < self.c.stable:
                    continue
                job = self.c.processing_dir / ("job-" + uuid.uuid4().hex)
                job.mkdir(mode=0o700)
                try:
                    move_file(path, job / path.name, expected=sig)
                except ClaimChanged:
                    if any(job.iterdir()):
                        atomic_json(
                            job / "result.json",
                            {
                                "filename": path.name,
                                "kind": classify(path),
                                "status": "error",
                                "error": "Ingress changed during claim; payload quarantined without upload",
                                "results": [],
                            },
                        )
                        self._process(job)
                    else:
                        job.rmdir()
                    self.seen.pop(path, None)
                    continue
                except Exception:
                    if not any(job.iterdir()):
                        job.rmdir()
                    raise
                self.seen.pop(path, None)
                self._process(job)
            except OSError:
                log.error("claim_error filename=%s status=error", json.dumps(path.name))
        self.seen = {p: v for p, v in self.seen.items() if p in current}

    def _process(self, job):
        marker = job / "result.json"
        if not marker.exists():
            files = [p for p in job.iterdir() if classify(p)]
            if not files:
                # Empty claim left by crash before rename.
                shutil.rmtree(job)
                return
            if len(files) != 1:
                log.error("invalid_processing_job status=error")
                return
            path = files[0]
            started = time.monotonic()
            result = {
                "filename": path.name,
                "kind": classify(path),
                "status": "success",
                "results": [],
            }
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    raise UploadError("Processing payload is not a regular file")
                result["results"] = self.uploader.upload(path)
            except UploadError as error:
                result.update(status="error", error=str(error), results=error.results)
            except Exception:  # noqa: BLE001 -- isolate a job without emitting arbitrary exceptions.
                result.update(
                    status="error", error="Unexpected uploader failure (details suppressed)"
                )
            result["duration"] = round(time.monotonic() - started, 3)
            try:
                atomic_json(marker, result)
            except OSError:
                # Fatal: cannot persist upload outcome. Stop rather than hot-loop reupload.
                raise RuntimeError(
                    "Cannot persist completion; stop and inspect processing directory"
                ) from None
            log.info("file_result %s", json.dumps(result, ensure_ascii=True))
        try:
            result = json.loads(marker.read_text())
            terminal = self.c.success_dir if result["status"] == "success" else self.c.error_dir
            target = terminal / job.name
            if target.exists() or target.is_symlink():
                if not identical_job(job, target):
                    log.error(
                        "terminal_collision job=%s processing_payload_preserved=true", job.name
                    )
                    return
            else:
                staging = terminal / ("." + job.name)
                if staging.exists():
                    shutil.rmtree(staging)
                shutil.copytree(job, staging, symlinks=True)
                for p in staging.iterdir():
                    if stat.S_ISREG(p.lstat().st_mode):
                        with p.open("rb") as stream:
                            os.fsync(stream.fileno())
                sync_directory(staging)
                os.rename(staging, target)
            sync_directory(terminal)
            shutil.rmtree(job)
            sync_directory(self.c.processing_dir)
        except OSError:
            log.error("completion_pending job=%s status=error", job.name)
