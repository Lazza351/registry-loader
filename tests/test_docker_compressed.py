"""Docker daemon accepts gzip-compressed docker-save archives through its socket."""

import gzip

from test_archives import archive

from nexus_uploader.config import Config
from nexus_uploader.upload import Uploader


def test_compressed_docker_archive_is_streamed_to_daemon(tmp_path, monkeypatch):
    original = archive(tmp_path / "source.tar", [{"RepoTags": ["team/app:v1"]}])
    compressed = tmp_path / "images.tar.gz"
    compressed.write_bytes(gzip.compress(original.read_bytes()))
    received = []

    class Image:
        def tag(self, *args, **kwargs):
            return True

    class Images:
        def load(self, stream):
            received.append(stream.read())

        def get(self, tag):
            return Image()

    class API:
        def push(self, *args, **kwargs):
            return iter([{"status": "ok"}])

    class Client:
        images, api = Images(), API()

        def close(self):
            pass

    monkeypatch.setattr("nexus_uploader.upload.docker.DockerClient", lambda **kwargs: Client())
    config = Config.from_env({"DOCKER_REGISTRY": "registry.example", "UPLOAD_RETRIES": "0"})
    assert Uploader(config).upload(compressed)[0]["status"] == "success"
    assert received == [compressed.read_bytes()]
    assert compressed.exists()
