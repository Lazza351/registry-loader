"""Regression for Bookworm skopeo: gzip needs decompression and writable --tmpdir."""

import gzip
import subprocess
import tarfile
from pathlib import Path

from test_archives import archive

from nexus_uploader.config import Config
from nexus_uploader.upload import Uploader


def test_compressed_docker_uses_uncompressed_source_and_explicit_tempdir(tmp_path, monkeypatch):
    original = archive(tmp_path / "source.tar", [{"RepoTags": ["team/app:v1"]}])
    compressed = tmp_path / "images.tar.gz"
    compressed.write_bytes(gzip.compress(original.read_bytes()))
    sources = []

    def run(argv, **kwargs):
        assert "--tmpdir" in argv
        assert Path(argv[argv.index("--tmpdir") + 1]).is_dir()
        source = Path(argv[-2].removeprefix("docker-archive:").removesuffix(":team/app:v1"))
        sources.append(source)
        assert source != compressed
        with tarfile.open(source, "r:") as tf:
            assert "manifest.json" in tf.getnames()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    config = Config.from_env({"DOCKER_REGISTRY": "registry.example", "UPLOAD_RETRIES": "0"})
    assert Uploader(config).upload(compressed)[0]["status"] == "success"
    assert sources and all(not p.exists() for p in sources)
    assert compressed.exists()
