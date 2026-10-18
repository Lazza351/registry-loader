import json

import pytest

from nexus_uploader.config import Config
from nexus_uploader.upload import UploadError
from nexus_uploader.watcher import Watcher


def config(tmp):
    return Config.from_env(
        {
            k: str(tmp / n)
            for k, n in [
                ("INPUT_DIR", "in"),
                ("PROCESSING_DIR", "work"),
                ("SUCCESS_DIR", "ok"),
                ("ERROR_DIR", "err"),
            ]
        }
    )


class FakeUploader:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def upload(self, path):
        self.calls.append(path.read_bytes())
        if self.fail:
            raise UploadError("failed", [{"tag": "app:v", "status": "error"}])
        return [{"status": "success"}]


def test_filesystem_stability_claim_completion(tmp_path):
    c = config(tmp_path)
    u = FakeUploader()
    with Watcher(c, u) as w:
        p = c.input_dir / "a.whl"
        p.write_bytes(b"first")
        (c.input_dir / "ignored.whl.part").write_bytes(b"pending")
        (c.input_dir / "link.whl").symlink_to(p)
        w.scan(now=0)
        w.scan(now=5)
        assert not u.calls
        p.write_bytes(b"finished")
        w.scan(now=10)
        w.scan(now=21)
        assert u.calls == [b"finished"]
        assert not p.exists()
        assert len(list(c.success_dir.glob("*/a.whl"))) == 1
        p.write_bytes(b"new producer file")
        w.scan(now=22)
        w.scan(now=33)
        assert len(list(c.success_dir.glob("*/a.whl"))) == 2
        assert (c.input_dir / "ignored.whl.part").exists()


def test_error_sidecar_and_restart_pickup(tmp_path):
    c, u = config(tmp_path), FakeUploader(fail=True)
    with Watcher(c, u):
        (c.input_dir / "bad.tar").write_bytes(b"bad")
    with Watcher(c, u) as w:
        w.scan(now=0)
        w.scan(now=11)
    result = list(c.error_dir.glob("*/result.json"))
    assert len(result) == 1
    assert json.loads(result[0].read_text())["results"][0]["status"] == "error"
    assert not list(c.processing_dir.glob("job-*"))


def test_single_instance_lock(tmp_path):
    c = config(tmp_path)
    with (
        Watcher(c, FakeUploader()),
        pytest.raises(RuntimeError, match="already running"),
        Watcher(c, FakeUploader()),
    ):
        pass
