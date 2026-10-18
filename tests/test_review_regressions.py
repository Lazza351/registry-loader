"""Crash, collision and ingress replacement regressions from independent review."""

import errno
import json
import os
import shutil

import pytest
from test_watcher import FakeUploader, config

from nexus_uploader.watcher import Watcher, move_file


def test_foreign_terminal_collision_keeps_processing_payload(tmp_path):
    c, uploader = config(tmp_path), FakeUploader()
    with Watcher(c, uploader) as watcher:
        job = c.processing_dir / "job-collision"
        job.mkdir()
        (job / "original.whl").write_bytes(b"original")
        (job / "result.json").write_text(json.dumps({"status": "success"}))
        foreign = c.success_dir / job.name
        foreign.mkdir()
        (foreign / "other.whl").write_bytes(b"foreign")
        watcher.scan()
        assert (job / "original.whl").read_bytes() == b"original"
        assert (foreign / "other.whl").read_bytes() == b"foreign"
        assert not uploader.calls


def test_interrupted_cross_device_copy_is_not_recovered_as_complete(tmp_path, monkeypatch):
    c, uploader = config(tmp_path), FakeUploader()

    class Crash(BaseException):
        pass

    with Watcher(c, uploader):
        source = c.input_dir / "original.whl"
        source.write_bytes(b"complete original")
        job = c.processing_dir / "job-interrupted"
        job.mkdir()
        with monkeypatch.context() as patcher:

            def exdev(*args):
                raise OSError(errno.EXDEV, "cross device")

            def interrupted(src, dst, **kwargs):
                dst.write(b"partial")
                raise Crash()

            patcher.setattr(os, "rename", exdev)
            patcher.setattr(shutil, "copyfileobj", interrupted)
            with pytest.raises(Crash):
                move_file(source, job / source.name)
    with Watcher(c, uploader) as watcher:
        watcher.scan(now=0)
    assert not uploader.calls
    assert source.read_bytes() == b"complete original"


def test_symlink_replacement_during_rename_never_reads_external_file(tmp_path, monkeypatch):
    c, uploader = config(tmp_path), FakeUploader()
    outside = tmp_path / "outside"
    outside.write_bytes(b"PRIVATE EXTERNAL DATA")
    rename = os.rename
    with Watcher(c, uploader) as watcher:
        source = c.input_dir / "original.whl"
        source.write_bytes(b"original")
        watcher.scan(now=0)

        def replace_before_rename(src, dst):
            if src == source:
                source.unlink()
                source.symlink_to(outside)
            return rename(src, dst)

        monkeypatch.setattr(os, "rename", replace_before_rename)
        watcher.scan(now=11)
    assert not uploader.calls
    assert outside.read_bytes() == b"PRIVATE EXTERNAL DATA"
    assert not list(c.success_dir.glob("job-*"))


def test_identical_published_job_is_cleaned_without_reupload(tmp_path):
    c, uploader = config(tmp_path), FakeUploader()
    with Watcher(c, uploader) as watcher:
        job = c.processing_dir / "job-published"
        job.mkdir()
        (job / "original.whl").write_bytes(b"original")
        (job / "result.json").write_text(json.dumps({"status": "success"}))
        shutil.copytree(job, c.success_dir / job.name)
        watcher.scan()
        assert not job.exists()
        assert (c.success_dir / job.name / "original.whl").read_bytes() == b"original"
        assert not uploader.calls


def test_same_names_different_bytes_are_not_considered_published(tmp_path):
    c, uploader = config(tmp_path), FakeUploader()
    with Watcher(c, uploader) as watcher:
        job = c.processing_dir / "job-different"
        job.mkdir()
        (job / "original.whl").write_bytes(b"original")
        (job / "result.json").write_text(json.dumps({"status": "success"}))
        target = c.success_dir / job.name
        shutil.copytree(job, target)
        (target / "original.whl").write_bytes(b"foreign!")
        watcher.scan()
        assert (job / "original.whl").read_bytes() == b"original"
        assert (target / "original.whl").read_bytes() == b"foreign!"
        assert not uploader.calls


def test_recovered_symlink_is_quarantined_without_following(tmp_path):
    c, uploader = config(tmp_path), FakeUploader()
    outside = tmp_path / "private"
    outside.write_bytes(b"private")
    with Watcher(c, uploader) as watcher:
        job = c.processing_dir / "job-symlink"
        job.mkdir()
        (job / "original.whl").symlink_to(outside)
        watcher.scan()
        assert not uploader.calls
        result = c.error_dir / job.name
        assert (result / "original.whl").is_symlink()
        assert json.loads((result / "result.json").read_text())["status"] == "error"
        assert outside.read_bytes() == b"private"
