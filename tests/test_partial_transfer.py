"""Ingress completion and payload integrity contracts, without remote services."""

import gzip
import io
import json
import os
import tarfile
import threading
import time
import zipfile
from dataclasses import replace

import pytest
from test_archives import archive
from test_upload import wheel
from test_watcher import FakeUploader, config

from nexus_uploader.ingress import IngressGuard, proc_writer_open, writer_open
from nexus_uploader.upload import Uploader
from nexus_uploader.watcher import Watcher


def test_stalled_open_producer_is_not_claimed(tmp_path):
    c, uploader = replace(config(tmp_path), stable=0.2), FakeUploader()
    started = threading.Event()
    errors = []

    def produce(path):
        try:
            with path.open("wb", buffering=0) as stream:
                stream.write(b"first")
                started.set()
                time.sleep(0.6)
                stream.write(b"second")
        except OSError as error:
            errors.append(error)
            started.set()

    with Watcher(c, uploader) as watcher:
        path = c.input_dir / "app.tar"
        thread = threading.Thread(target=produce, args=(path,))
        thread.start()
        try:
            assert started.wait(1)
            watcher.scan()
            time.sleep(0.25)
            watcher.scan()
            assert path.exists()
            assert not uploader.calls
        finally:
            thread.join(1)
        assert not errors
        watcher.scan()
        time.sleep(0.21)
        watcher.scan()
        assert uploader.calls == [b"firstsecond"]


class NoRemoteUploader(Uploader):
    def _docker(self, path):
        pytest.fail("Docker upload reached before integrity validation")

    def _pypi(self, path):
        pytest.fail("PyPI upload reached before integrity validation")


@pytest.mark.parametrize("kind", ["wheel", "zip-crc", "tar", "gzip-trailer", "gzip-body", "gzip-crc"])
def test_corrupt_payload_quarantined_before_upload(tmp_path, kind):
    c = config(tmp_path)
    with Watcher(c, NoRemoteUploader(c)) as watcher:
        if kind == "wheel":
            path = wheel(c.input_dir / "demo-1.0-py3-none-any.whl")
            data = path.read_bytes()
            path.write_bytes(data[: len(data) // 2])
        elif kind == "zip-crc":
            path = c.input_dir / "app.zip"
            with zipfile.ZipFile(path, "w") as stream:
                stream.writestr("payload", b"hello")
            data = bytearray(path.read_bytes())
            data[30 + len("payload")] ^= 1
            path.write_bytes(data)
        elif kind == "tar":
            path = archive(c.input_dir / "app.tar", [{"RepoTags": ["app:v1"]}])
            with tarfile.open(path) as stream:
                member = stream.getmembers()[-1]
                end = member.offset_data + ((member.size + 511) // 512) * 512
            path.write_bytes(path.read_bytes()[:end])
        else:
            path = c.input_dir / "app.tar.gz"
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as tf:
                member = tarfile.TarInfo("demo/PKG-INFO")
                member.size = 5
                tf.addfile(member, io.BytesIO(b"hello"))
            data = bytearray(gzip.compress(stream.getvalue()))
            if kind == "gzip-crc":
                data[-8] ^= 1
                path.write_bytes(data)
            else:
                path.write_bytes(data[:-8] if kind == "gzip-trailer" else data[: len(data) // 2])
        watcher.scan(now=0)
        watcher.scan(now=11)
        results = list(c.error_dir.glob("*/result.json"))
        assert len(results) == 1
        result = json.loads(results[0].read_text())
        assert result["status"] == "error"
        assert result["error"] == "Payload incomplete or corrupt; transfer was interrupted"
        assert not list(c.success_dir.glob("job-*"))


class QuietGuard:
    def __init__(self, supported=True, completed=False):
        self.available = supported
        self.done = completed
        self.forgotten = []

    def poll(self):
        pass

    def supported(self, directory):
        return self.available

    def completed(self, path):
        return self.done

    def forget(self, identity):
        self.forgotten.append(identity)

    def close(self):
        pass


@pytest.mark.parametrize(
    "completed,probe,supported,claim",
    [(True, None, True, True), (False, True, True, False),
     (False, False, True, True), (False, None, True, False),
     (False, None, False, True)],
)
def test_all_claim_policy_branches(tmp_path, completed, probe, supported, claim):
    c, uploader = config(tmp_path), FakeUploader()
    guard = QuietGuard(supported=supported, completed=completed)
    probes = []

    def check(path):
        probes.append(path)
        return probe

    with Watcher(c, uploader, guard=guard, probe=check) as watcher:
        path = c.input_dir / "app.tar"
        path.write_bytes(b"payload")
        identity = (path.stat().st_dev, path.stat().st_ino)
        watcher.scan(now=0)
        watcher.scan(now=9)
        assert not uploader.calls
        watcher.scan(now=11)
        assert bool(uploader.calls) is claim
        assert probes == ([] if completed else [path])
        assert guard.forgotten == ([identity] if claim else [])
        if probe is True and not completed:
            # The writer must restart the minimum quiet window, not just skip once.
            watcher.probe = lambda path: False
            watcher.scan(now=12)
            assert not uploader.calls
            watcher.scan(now=22)
            assert uploader.calls == [b"payload"]


def test_close_event_claims_next_scan_without_probe(tmp_path):
    c, uploader = config(tmp_path), FakeUploader()
    with Watcher(c, uploader, probe=lambda path: None) as watcher:
        assert watcher.guard.supported(c.input_dir)
        path = c.input_dir / "app.tar"
        with path.open("wb", buffering=0) as stream:
            stream.write(b"complete")
            watcher.scan(now=0)
            watcher.scan(now=11)
            assert path.exists()
            assert not uploader.calls
        watcher.scan(now=12)
        assert uploader.calls == [b"complete"]
        assert not watcher.guard.done


def test_moved_to_tracks_inode_and_forgets_claim(tmp_path):
    directory = tmp_path / "in"
    directory.mkdir()
    path = tmp_path / "app.tar"
    path.write_bytes(b"complete")
    guard = IngressGuard([directory])
    try:
        assert guard.supported(directory)
        target = directory / path.name
        path.rename(target)
        guard.poll()
        assert guard.completed(target)
        guard.forget((target.stat().st_dev, target.stat().st_ino))
        assert not guard.completed(target)
    finally:
        guard.close()
    assert guard.fd == -1


def test_real_writer_probe(tmp_path):
    import signal

    path = tmp_path / "payload"
    path.write_bytes(b"payload")
    assert writer_open(path) is False
    assert signal.getsignal(signal.SIGIO) == signal.SIG_IGN
    with path.open("ab"):
        assert writer_open(path) is True
        assert proc_writer_open(path) is True
    assert writer_open(path) is False


def test_proc_denied_is_unknown(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "payload"
    path.write_bytes(b"payload")
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    original = Path.iterdir

    def entries(directory):
        if directory == proc / "123" / "fd":
            raise PermissionError("not our process")
        return original(directory)

    monkeypatch.setattr(Path, "iterdir", entries)
    assert proc_writer_open(path, proc=proc) is None


def test_proc_flags_distinguish_readers_and_writers(tmp_path):
    proc = tmp_path / "proc"
    fd = proc / "123" / "fd"
    fdinfo = proc / "123" / "fdinfo"
    fd.mkdir(parents=True)
    fdinfo.mkdir()
    path = tmp_path / "payload"
    path.write_bytes(b"payload")
    (fd / "5").symlink_to(path)
    (fdinfo / "5").write_text("flags:\t0100000\n")
    assert proc_writer_open(path, proc=proc) is False
    (fdinfo / "5").write_text("flags:\t0100002\n")
    assert proc_writer_open(path, proc=proc) is True


@pytest.mark.parametrize("number", ["EPERM", "EINVAL", "ENOSYS", "EOPNOTSUPP"])
def test_denied_lease_falls_back_to_proc(tmp_path, monkeypatch, number):
    import errno

    from nexus_uploader import ingress

    path = tmp_path / "payload"
    path.write_bytes(b"payload")

    def denied(*args):
        raise OSError(getattr(errno, number), "lease unavailable")

    monkeypatch.setattr(ingress.fcntl, "fcntl", denied)
    with path.open("ab"):
        assert writer_open(path) is True


def test_overflow_disables_trust_once(tmp_path, caplog):
    from nexus_uploader import ingress

    guard = IngressGuard([tmp_path])
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    original = guard.fd
    try:
        assert guard.supported(tmp_path)
        path = tmp_path / "app.tar"
        path.write_bytes(b"payload")
        guard.poll()
        assert guard.completed(path)
        # Feed actual binary events through poll(), including the nameless wd=-1 event.
        guard.fd = read_fd
        event = ingress.EVENT.pack(-1, ingress.IN_Q_OVERFLOW, 0, 0)
        os.write(write_fd, event + event)
        guard.poll()
        assert guard.degraded
        assert not guard.supported(tmp_path)
        assert not guard.completed(path)
        assert caplog.text.count("falling back to stability checks") == 1
    finally:
        guard.close()
        os.close(write_fd)
        os.close(original)


@pytest.mark.parametrize("kind", ["nfs", "nfs4", "cifs", "smb3", "9p", "fuse", "fuse.sshfs"])
def test_remote_filesystem_is_unsupported(tmp_path, monkeypatch, kind):
    from nexus_uploader import ingress

    monkeypatch.setattr(ingress, "filesystem_type", lambda directory: kind)
    guard = IngressGuard([tmp_path])
    try:
        assert not guard.supported(tmp_path)
    finally:
        guard.close()


def test_mountinfo_uses_deepest_mount_and_unescapes(tmp_path):
    from nexus_uploader.ingress import filesystem_type

    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "1 0 0:1 / / rw - ext4 /dev/root rw\n"
        "2 1 0:2 / /data rw - nfs4 server:/share rw\n"
        "3 2 0:3 / /data/local\\040files rw - xfs /dev/local rw\n"
    )
    assert filesystem_type("/data/remote", mountinfo) == "nfs4"
    assert filesystem_type("/data/local files/subdir", mountinfo) == "xfs"
    assert filesystem_type("/database", mountinfo) == "ext4"


def test_inotify_initialization_failure_is_nonfatal(tmp_path, monkeypatch):
    from nexus_uploader import ingress

    def unavailable(*args, **kwargs):
        raise OSError("libc unavailable")

    monkeypatch.setattr(ingress.ctypes, "CDLL", unavailable)
    c, uploader = config(tmp_path), FakeUploader()
    with Watcher(c, uploader, probe=lambda path: None) as watcher:
        assert not watcher.guard.supported(c.input_dir)
        (c.input_dir / "app.tar").write_bytes(b"payload")
        watcher.scan(now=0)
        watcher.scan(now=11)
        assert uploader.calls == [b"payload"]


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz", ".whl", ".zip"])
def test_complete_payload_passes_integrity_gate(tmp_path, suffix):
    from nexus_uploader.archives import classify, verify_payload

    path = tmp_path / ("app" + suffix)
    if suffix in {".tar", ".tar.gz"}:
        archive(path, [{"RepoTags": ["app:v1"]}])
    else:
        wheel(path)
    verify_payload(path, classify(path))


def test_watch_support_is_per_directory(tmp_path):
    missing = tmp_path / "not-created"
    guard = IngressGuard([tmp_path, missing])
    try:
        assert guard.supported(tmp_path)
        assert not guard.supported(missing)
        path = tmp_path / "vanished.tar"
        path.write_bytes(b"payload")
        path.unlink()
        guard.poll()
        assert not guard.done
    finally:
        guard.close()



def test_preexisting_entry_is_claimed_without_a_close_event(tmp_path):
    """A file found at startup never gets IN_CLOSE_WRITE; it must not wait forever."""
    c, uploader = config(tmp_path), FakeUploader()
    c.input_dir.mkdir(parents=True)
    (c.input_dir / "app.tar").write_bytes(b"payload")
    guard = QuietGuard(supported=True, completed=False)
    with Watcher(c, uploader, guard=guard, probe=lambda path: None) as watcher:
        watcher.scan(now=0)
        assert not uploader.calls
        watcher.scan(now=11)
        assert uploader.calls == [b"payload"]


def test_entry_created_under_watch_waits_for_its_close_event(tmp_path):
    """The same unavailable probe must still veto an entry that appeared after startup."""
    c, uploader = config(tmp_path), FakeUploader()
    guard = QuietGuard(supported=True, completed=False)
    with Watcher(c, uploader, guard=guard, probe=lambda path: None) as watcher:
        (c.input_dir / "app.tar").write_bytes(b"payload")
        watcher.scan(now=0)
        watcher.scan(now=11)
        assert not uploader.calls
        guard.done = True
        watcher.scan(now=12)
        assert uploader.calls == [b"payload"]


def test_close_marks_expire_so_ignored_names_cannot_accumulate(tmp_path):
    from nexus_uploader import ingress

    directory = tmp_path / "in"
    directory.mkdir()
    guard = IngressGuard([directory])
    try:
        assert guard.supported(directory)
        guard.done[(1, 2)] = 0.0
        guard.poll(now=ingress.CLOSED_TTL + 1)
        assert not guard.done
        guard.done[(1, 3)] = 100.0
        guard.poll(now=100.5)
        assert (1, 3) in guard.done
    finally:
        guard.close()
