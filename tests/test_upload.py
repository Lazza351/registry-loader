import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from docker.errors import DockerException
from test_archives import archive

from nexus_uploader.config import Config
from nexus_uploader.upload import Uploader, UploadError


def wheel(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "demo-1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"
        )
        z.writestr(
            "demo-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
    return path


def test_real_pypi_upload_retry_and_auth(tmp_path, caplog):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(
                (self.path, self.headers, self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(503 if len(received) == 1 else 200, "REMOTE_PASSWORD_SECRET")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        c = Config.from_env(
            {
                "PYPI_URL": f"http://127.0.0.1:{server.server_port}/repository/pypi/",
                "PYPI_USERNAME": "test",
                "PYPI_PASSWORD": "secret",
                "UPLOAD_RETRIES": "1",
            }
        )
        Uploader(c).upload(wheel(tmp_path / "demo-1.0-py3-none-any.whl"))
        assert len(received) == 2
        assert "REMOTE_PASSWORD_SECRET" not in caplog.text
        assert received[-1][0] == "/repository/pypi/"
        assert received[-1][1]["Authorization"].startswith("Basic ")
        assert b'name="content"; filename="demo-1.0-py3-none-any.whl"' in received[-1][2]
        assert b"file_upload" in received[-1][2]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_docker_socket_load_retag_and_push_all_tags(tmp_path, monkeypatch):
    c = Config.from_env(
        {
            "DOCKER_REGISTRY": "nexus.test:5000",
            "DOCKER_USERNAME": "u",
            "DOCKER_PASSWORD": "TOPSECRET",
            "IMAGE_PREFIX": "mirror",
        }
    )
    calls = []

    class Image:
        def tag(self, repository, tag, force):
            calls.append(("tag", repository, tag, force))
            return True

    class Images:
        def load(self, stream):
            calls.append(("load", stream.read()))

        def get(self, tag):
            calls.append(("get", tag))
            return Image()

    class API:
        def push(self, repository, tag, auth_config, stream, decode):
            calls.append(("push", repository, tag, auth_config, stream, decode))
            return iter([{"status": "pushed"}])

    class Client:
        images = Images()
        api = API()

        def close(self):
            calls.append(("close",))

    def create_client(**kwargs):
        assert kwargs == {
            "base_url": "unix:///var/run/docker.sock", "timeout": 300, "version": "auto"
        }
        return Client()

    monkeypatch.setattr("nexus_uploader.upload.docker.DockerClient", create_client)
    p = archive(tmp_path / "images.tar", [{"RepoTags": ["team/app:v1", "team/app:latest"]}])
    result = Uploader(c).upload(p)
    assert len(result) == 2
    assert calls[0][0] == "load"
    assert ("tag", "nexus.test:5000/mirror/team/app", "v1", True) in calls
    assert (
        "push", "nexus.test:5000/mirror/team/app", "v1",
        {"username": "u", "password": "TOPSECRET"}, True, True
    ) in calls
    assert calls[-1] == ("close",)


def test_partial_failure_is_sanitized(tmp_path, monkeypatch):
    c = Config.from_env({"DOCKER_REGISTRY": "nexus.test", "UPLOAD_RETRIES": "0"})
    n = 0

    class Image:
        def tag(self, *args, **kwargs):
            return True

    class Images:
        def load(self, stream):
            stream.read()

        def get(self, tag):
            return Image()

    class API:
        def push(self, *args, **kwargs):
            nonlocal n
            n += 1
            if n == 2:
                return iter([{"error": "password=secret"}])
            return iter([{"status": "ok"}])

    class Client:
        images, api = Images(), API()

        def close(self):
            pass

    monkeypatch.setattr("nexus_uploader.upload.docker.DockerClient", lambda **kwargs: Client())
    p = archive(tmp_path / "a.tar", [{"RepoTags": ["a:one", "a:two", "a:three"]}])
    with pytest.raises(UploadError) as caught:
        Uploader(c).upload(p)
    assert [r["status"] for r in caught.value.results] == ["success", "error", "success"]
    assert "secret" not in str(caught.value)


def test_timeout_and_missing_config_are_sanitized(tmp_path, monkeypatch):
    p = archive(tmp_path / "a.tar", [{"RepoTags": ["a:one"]}])
    with pytest.raises(UploadError):
        Uploader(Config.from_env({})).upload(p)

    monkeypatch.setattr(
        "nexus_uploader.upload.docker.DockerClient",
        lambda **kwargs: (_ for _ in ()).throw(DockerException("secret")),
    )
    with pytest.raises(UploadError, match="Docker daemon load failed"):
        Uploader(Config.from_env({"DOCKER_REGISTRY": "host", "UPLOAD_RETRIES": "0"})).upload(p)
