"""Real queue recovery and cross-device copy fault tests."""

import errno
import os
import shutil
from pathlib import Path

import pytest
from test_watcher import FakeUploader, config

from nexus_uploader.watcher import Watcher, move_file


def test_completed_job_retries_finalize_without_reupload(tmp_path, monkeypatch):
    c, u = config(tmp_path), FakeUploader()
    original = shutil.copytree
    with Watcher(c, u) as w:
        (c.input_dir / "x.whl").write_bytes(b"original")
        w.scan(now=0)

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copytree", fail)
        w.scan(now=11)
        w.scan(now=22)
        assert len(u.calls) == 1
        assert len(list(c.processing_dir.glob("job-*/result.json"))) == 1
    monkeypatch.setattr(shutil, "copytree", original)
    with Watcher(c, u) as w:
        w.scan(now=30)
    assert len(u.calls) == 1
    assert len(list(c.success_dir.glob("*/x.whl"))) == 1


def test_crash_before_result_reuploads_at_least_once(tmp_path):
    c, u = config(tmp_path), FakeUploader()
    with Watcher(c, u):
        job = c.processing_dir / "job-recovery"
        job.mkdir()
        (job / "x.whl").write_bytes(b"crash")
    with Watcher(c, u) as w:
        w.scan()
    assert u.calls == [b"crash"]


def test_cross_filesystem_copy_before_delete(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")

    def exdev(*args):
        raise OSError(errno.EXDEV, "different device")

    monkeypatch.setattr(os, "rename", exdev)
    move_file(source, target)
    assert target.read_bytes() == b"payload"
    assert not source.exists()


def test_cross_filesystem_copy_failure_keeps_source(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")

    def exdev(*args):
        raise OSError(errno.EXDEV, "different device")

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "rename", exdev)
    monkeypatch.setattr(shutil, "copyfileobj", broken)
    with pytest.raises(OSError):
        move_file(source, target)
    assert source.read_bytes() == b"payload"
    assert not target.exists()


def test_real_cross_filesystem_move(tmp_path):
    import tempfile

    if not Path("/dev/shm").is_dir() or os.stat("/dev/shm").st_dev == os.stat(tmp_path).st_dev:
        pytest.skip("No second filesystem")
    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="nexus-test-") as second:
        source = tmp_path / "source"
        source.write_bytes(b"real EXDEV")
        target = Path(second) / "target"
        move_file(source, target)
        assert target.read_bytes() == b"real EXDEV"
        assert not source.exists()


def test_marker_write_failure_stops_instead_of_hot_loop(tmp_path, monkeypatch):
    import nexus_uploader.watcher as module

    c, u = config(tmp_path), FakeUploader()
    with Watcher(c, u) as w:
        (c.input_dir / "x.whl").write_bytes(b"data")
        w.scan(now=0)

        def broken(*args):
            raise OSError("full")

        monkeypatch.setattr(module, "atomic_json", broken)
        with pytest.raises(RuntimeError, match="Cannot persist completion"):
            w.scan(now=11)
    assert len(u.calls) == 1
