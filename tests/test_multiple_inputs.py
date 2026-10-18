from dataclasses import replace

import pytest

from nexus_uploader.config import Config
from nexus_uploader.watcher import Watcher


def multiple_config(tmp_path):
    return Config.from_env(
        {
            "INPUT_DIRS": f"{tmp_path}/incoming-a,{tmp_path}/incoming-b",
            "PROCESSING_DIR": str(tmp_path / "processing"),
            "SUCCESS_DIR": str(tmp_path / "success"),
            "ERROR_DIR": str(tmp_path / "error"),
            "STABLE_SECONDS": "0.1",
        }
    )


class Accept:
    def upload(self, path):
        return [{"status": "success"}]


def test_multiple_directories_with_same_filename_do_not_overwrite(tmp_path):
    c = multiple_config(tmp_path)
    assert len(c.watch_dirs) == 2
    with Watcher(c, Accept()) as watcher:
        for directory in c.watch_dirs:
            (directory / "same.whl").write_bytes(b"fixture")
        watcher.scan(now=1)
        watcher.scan(now=2)
    assert len(list(c.success_dir.glob("job-*/same.whl"))) == 2
    assert all(not list(d.glob("*.whl")) for d in c.watch_dirs)


def test_multiple_input_paths_must_not_overlap(tmp_path):
    with pytest.raises(ValueError):
        Config.from_env({"INPUT_DIRS": f"{tmp_path}/a,{tmp_path}/a/b"})


def test_shared_processing_queue_prevents_second_worker(tmp_path):
    c = multiple_config(tmp_path)
    other = replace(c, input_dir=tmp_path / "other", extra_input_dirs=())
    with (
        Watcher(c, Accept()),
        pytest.raises(RuntimeError, match="already running"),
        Watcher(other, Accept()),
    ):
        pass
