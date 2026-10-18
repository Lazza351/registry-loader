import json
import os
import stat
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
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


def test_skopeo_all_tags_private_authfile_cleanup(tmp_path, monkeypatch):
    c = Config.from_env(
        {
            "DOCKER_REGISTRY": "nexus.test:5000",
            "DOCKER_USERNAME": "u",
            "DOCKER_PASSWORD": "TOPSECRET",
            "IMAGE_PREFIX": "mirror",
            "DOCKER_TLS_VERIFY": "false",
        }
    )
    calls, authfiles = [], []

    def run(argv, **kwargs):
        calls.append(argv)
        auth = argv[argv.index("--authfile") + 1]
        authfiles.append(auth)
        assert stat.S_IMODE(os.stat(auth).st_mode) == 0o600
        assert "nexus.test:5000" in json.loads(Path(auth).read_text())["auths"]
        assert "TOPSECRET" not in str(argv)
        assert kwargs["timeout"] == 300
        assert kwargs["stdout"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    p = archive(tmp_path / "images.tar", [{"RepoTags": ["team/app:v1", "team/app:latest"]}])
    result = Uploader(c).upload(p)
    assert len(result) == 2
    assert calls[0][-1] == "docker://nexus.test:5000/mirror/team/app:v1"
    assert calls[0][-2] == f"docker-archive:{p}:team/app:v1"
    assert "--dest-tls-verify=false" in calls[0]
    assert all(not os.path.exists(p) for p in authfiles)


def test_partial_failure_is_sanitized(tmp_path, monkeypatch):
    c = Config.from_env({"DOCKER_REGISTRY": "nexus.test", "UPLOAD_RETRIES": "0"})
    n = 0

    def run(argv, **kwargs):
        nonlocal n
        n += 1
        if n == 2:
            raise subprocess.CalledProcessError(1, argv, stderr="password=secret")

    monkeypatch.setattr(subprocess, "run", run)
    p = archive(tmp_path / "a.tar", [{"RepoTags": ["a:one", "a:two", "a:three"]}])
    with pytest.raises(UploadError) as caught:
        Uploader(c).upload(p)
    assert [r["status"] for r in caught.value.results] == ["success", "error", "success"]
    assert "secret" not in str(caught.value)


def test_timeout_and_missing_config_are_sanitized(tmp_path, monkeypatch):
    p = archive(tmp_path / "a.tar", [{"RepoTags": ["a:one"]}])
    with pytest.raises(UploadError):
        Uploader(Config.from_env({})).upload(p)

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1, output="secret")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(UploadError, match="Docker upload failed"):
        Uploader(Config.from_env({"DOCKER_REGISTRY": "host", "UPLOAD_RETRIES": "0"})).upload(p)
