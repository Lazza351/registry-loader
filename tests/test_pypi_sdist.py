"""Exercise sdists and HTTP rejection through the real Twine upload path."""

import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexus_uploader.config import Config
from nexus_uploader.upload import Uploader, UploadError


@pytest.mark.parametrize("status", [200, 302, 403])
def test_real_source_distribution_upload_and_no_redirect(tmp_path, status):
    path = tmp_path / "demo-1.0.tar.gz"
    metadata = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nSummary: Fixture\n"
    with tarfile.open(path, "w:gz") as archive:
        directory = tarfile.TarInfo("demo-1.0")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("demo-1.0/PKG-INFO")
        member.size = len(metadata)
        archive.addfile(member, io.BytesIO(metadata))
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(status)
            if status == 302:
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/unexpected/"
                )
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = Config.from_env(
            {
                "PYPI_URL": f"http://127.0.0.1:{server.server_port}/repository/pypi/",
                "UPLOAD_RETRIES": "0",
            }
        )
        if status == 200:
            assert Uploader(config).upload(path)[0]["status"] == "success"
        else:
            with pytest.raises(UploadError, match=f"HTTP {status}"):
                Uploader(config).upload(path)
        assert len(received) == 1
        assert b'filename="demo-1.0.tar.gz"' in received[0]
        assert b'name="filetype"\r\n\r\nsdist' in received[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
