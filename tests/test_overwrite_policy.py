"""Overwrite configuration, probes and upload policy contracts."""

import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from test_archives import archive
from test_upload import wheel
from test_watcher import config as watcher_config

from nexus_uploader.config import Config
from nexus_uploader.upload import Uploader, UploadError
from nexus_uploader.watcher import Watcher


@pytest.fixture
def docker_client(monkeypatch):
    from types import SimpleNamespace

    calls = []
    image = SimpleNamespace(tag=lambda *args, **kwargs: True)
    images = SimpleNamespace(load=lambda stream: stream.read(), get=lambda tag: image)

    def push(repository, **kwargs):
        calls.append((repository, kwargs["tag"]))
        return iter([{"status": "ok"}])

    client = SimpleNamespace(images=images, api=SimpleNamespace(push=push), close=lambda: None)
    monkeypatch.setattr("nexus_uploader.upload.docker.DockerClient", lambda **kwargs: client)
    return calls


@pytest.mark.parametrize(
    "exists, overwrite", [(True, False), (True, True), (False, False), (False, True)]
)
def test_docker_overwrite_policy(tmp_path, nexus, docker_client, exists, overwrite):
    nexus["status"] = 200 if exists else 404
    c = Config.from_env(
        {"DOCKER_REGISTRY": nexus["host"], "TLS_VERIFY": "false", "ALLOW_OVERWRITE": str(overwrite)}
    )
    path = archive(tmp_path / "image.tar", [{"RepoTags": ["team/app:v1"]}])
    if exists and not overwrite:
        with pytest.raises(UploadError, match="Artifact already exists") as caught:
            Uploader(c).upload(path)
        assert caught.value.results[0]["error"] == "Artifact already exists"
        assert caught.value.results[0]["status"] == "error"
        assert not docker_client
    else:
        assert Uploader(c).upload(path)[0]["status"] == "success"
        assert docker_client == [(nexus["host"] + "/team/app", "v1")]


def test_enabled_overwrite_never_probes_the_registry(tmp_path, nexus, docker_client, monkeypatch):
    def fail(*args):
        pytest.fail("existence probe must not run when overwriting is allowed")

    monkeypatch.setattr("nexus_uploader.presence.RegistryProbe.tag_exists", fail)
    c = Config.from_env(
        {"DOCKER_REGISTRY": nexus["host"], "ALLOW_OVERWRITE": "true", "TLS_VERIFY": "false"}
    )
    path = archive(tmp_path / "image.tar", [{"RepoTags": ["team/app:v1"]}])
    assert Uploader(c).upload(path)[0]["status"] == "success"
    assert docker_client == [(nexus["host"] + "/team/app", "v1")]


@pytest.mark.parametrize(
    "exists, overwrite, delete_status",
    [
        (True, False, 204),
        (True, True, 204),
        (True, True, 403),
        (False, False, 204),
        (False, True, 204),
    ],
)
def test_pypi_overwrite_policy(tmp_path, nexus, exists, overwrite, delete_status):
    nexus.update(
        items=[{"id": "one"}, {"id": "two"}] if exists else [], delete_status=delete_status
    )
    c = Config.from_env(
        {"PYPI_URL": nexus["url"] + "/repository/python/", "ALLOW_OVERWRITE": str(overwrite)}
    )
    path = wheel(tmp_path / "demo-1.0-py3-none-any.whl")
    if exists and (not overwrite or delete_status == 403):
        message = (
            "Artifact already exists in the repository" if not overwrite else "Overwrite denied"
        )
        with pytest.raises(UploadError, match=message):
            Uploader(c).upload(path)
        assert not [call for call in nexus["calls"] if call[0] == "POST"]
    else:
        assert Uploader(c).upload(path)[0]["status"] == "success"
        methods = [call[0] for call in nexus["calls"]]
        assert methods == (["GET", "DELETE", "DELETE", "POST"] if exists else ["GET", "POST"])
    search = parse_qs(urlsplit(nexus["calls"][0][1]).query)
    assert search["name"] == ["demo"]
    assert search["version"] == ["1.0"]


def test_pypi_unavailable_warns_once(tmp_path, nexus, caplog, monkeypatch):
    monkeypatch.setattr("nexus_uploader.upload._presence_warning_logged", False, raising=False)
    c = Config.from_env({"PYPI_URL": nexus["url"] + "/upload/"})
    path = wheel(tmp_path / "demo-1.0-py3-none-any.whl")
    assert Uploader(c).upload(path)[0]["status"] == "success"
    assert Uploader(c).upload(path)[0]["status"] == "success"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "existence check is unavailable" in warnings[0].message
    assert [call[0] for call in nexus["calls"]] == ["POST", "POST"]


def test_watcher_preserves_rejected_payload(tmp_path, nexus):
    nexus["items"] = [{"id": "one"}]
    c = replace(
        watcher_config(tmp_path),
        pypi_url=nexus["url"] + "/repository/python/",
        nexus_api_url=nexus["url"],
    )
    with Watcher(c, Uploader(c)) as watcher:
        path = wheel(c.input_dir / "demo-1.0-py3-none-any.whl")
        payload = path.read_bytes()
        watcher.scan(now=0)
        watcher.scan(now=11)
    results = list(c.error_dir.glob("*/result.json"))
    assert len(results) == 1
    result = json.loads(results[0].read_text())
    assert result["error"] == "Artifact already exists in the repository"
    assert (results[0].parent / path.name).read_bytes() == payload
    assert not list(c.success_dir.glob("job-*"))


@pytest.mark.parametrize("kind", ["docker", "pypi"])
def test_missing_repository_does_not_probe(tmp_path, monkeypatch, kind):
    def fail(*args, **kwargs):
        pytest.fail("unconfigured repository must not probe")

    monkeypatch.setattr("nexus_uploader.presence.RegistryProbe.tag_exists", fail)
    monkeypatch.setattr("nexus_uploader.presence.PyPIProbe.component_ids", fail)
    path = (
        archive(tmp_path / "image.tar", [{"RepoTags": ["app:v1"]}])
        if kind == "docker"
        else wheel(tmp_path / "demo-1.0-py3-none-any.whl")
    )
    with pytest.raises(UploadError, match="is not configured"):
        Uploader(Config.from_env({})).upload(path)


def test_check_config_does_not_probe(tmp_path, monkeypatch):
    from nexus_uploader.cli import main

    def fail(*args, **kwargs):
        pytest.fail("--check-config must not access network or Docker")

    monkeypatch.setattr("sys.argv", ["nexus-auto-uploader", "--check-config"])
    monkeypatch.setenv("DOCKER_REGISTRY", "host")
    monkeypatch.setenv("PYPI_URL", "https://host/repository/python/")
    monkeypatch.setenv("ALLOW_OVERWRITE", "true")
    for key, name in [
        ("INPUT_DIR", "in"),
        ("PROCESSING_DIR", "work"),
        ("SUCCESS_DIR", "ok"),
        ("ERROR_DIR", "err"),
    ]:
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setattr("nexus_uploader.upload.TimeoutSession.request", fail)
    monkeypatch.setattr("nexus_uploader.upload.docker.DockerClient", fail)
    assert main() == 0
    assert not list(tmp_path.iterdir())


def test_docker_mixed_existing_tags(tmp_path, nexus, docker_client, monkeypatch):
    checked = []

    def exists(self, repository, tag):
        checked.append((repository, tag))
        return tag == "existing"

    monkeypatch.setattr("nexus_uploader.presence.RegistryProbe.tag_exists", exists)
    c = Config.from_env({"DOCKER_REGISTRY": nexus["host"], "IMAGE_PREFIX": "mirror"})
    path = archive(tmp_path / "image.tar", [{"RepoTags": ["team/app:existing", "team/app:new"]}])
    with pytest.raises(UploadError, match="Docker upload rejected; see per-tag results") as caught:
        Uploader(c).upload(path)
    assert checked == [("mirror/team/app", "existing"), ("mirror/team/app", "new")]
    # One conflicting tag rejects the whole archive: no partial push, nothing loaded locally.
    assert [r["tag"] for r in caught.value.results] == ["team/app:existing"]
    assert docker_client == []


@pytest.fixture
def nexus():
    state = {"status": 200, "items": [], "calls": [], "delete_status": 204}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["calls"].append(("GET", self.path, dict(self.headers)))
            self.send_response(state["status"])
            self.end_headers()
            payload = (
                state["pages"].pop(0)
                if state.get("pages")
                else {"items": state["items"], "continuationToken": None}
            )
            self.wfile.write(json.dumps(payload).encode())

        def do_DELETE(self):
            state["calls"].append(("DELETE", self.path, dict(self.headers)))
            self.send_response(state["delete_status"])
            self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            state["calls"].append(("POST", self.path, dict(self.headers)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state["host"] = f"127.0.0.1:{server.server_port}"
    state["url"] = "http://" + state["host"]
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "status, expected", [(200, True), (404, False), (401, None), (403, None), (500, None)]
)
def test_registry_probe(nexus, status, expected):
    from nexus_uploader.presence import RegistryProbe

    nexus["status"] = status
    c = Config.from_env(
        {
            "DOCKER_REGISTRY": nexus["host"],
            "TLS_VERIFY": "false",
            "DOCKER_USERNAME": "user",
            "DOCKER_PASSWORD": "secret",
        }
    )
    if expected is None:
        message = (
            "Registry authentication failed"
            if status in {401, 403}
            else "Registry existence check failed"
        )
        with pytest.raises(UploadError, match=message):
            RegistryProbe(c).tag_exists("team/app", "latest")
    else:
        assert RegistryProbe(c).tag_exists("team/app", "latest") is expected
    _, path, headers = nexus["calls"][-1]
    assert path == "/v2/team/app/manifests/latest"
    assert headers["Authorization"].startswith("Basic ")
    assert "application/vnd.oci.image.manifest.v1+json" in headers["Accept"]
    assert "application/vnd.docker.distribution.manifest.list.v2+json" in headers["Accept"]


@pytest.mark.parametrize(
    "items, status, expected",
    [
        ([{"id": "one"}, {"id": "two"}], 200, ["one", "two"]),
        ([], 200, []),
        ([], 404, []),
        ([], 403, None),
    ],
)
def test_pypi_search(nexus, items, status, expected):
    from nexus_uploader.presence import PyPIProbe

    nexus.update(items=items, status=status)
    c = Config.from_env(
        {
            "PYPI_URL": nexus["url"] + "/repository/python/",
            "PYPI_USERNAME": "user",
            "PYPI_PASSWORD": "secret",
        }
    )
    if expected is None:
        with pytest.raises(UploadError, match=r"Nexus API request failed \(HTTP 403\)"):
            PyPIProbe(c).component_ids("My__Project.Name", "1.0")
    else:
        assert PyPIProbe(c).component_ids("My__Project.Name", "1.0") == expected
    _, path, headers = nexus["calls"][-1]
    assert urlsplit(path).path == "/service/rest/v1/search"
    assert parse_qs(urlsplit(path).query) == {
        "repository": ["python"],
        "name": ["my-project-name"],
        "version": ["1.0"],
    }
    assert headers["Authorization"].startswith("Basic ")


@pytest.mark.parametrize("status", [200, 204, 403, 500])
def test_pypi_delete(nexus, status):
    from nexus_uploader.presence import PyPIProbe

    nexus["delete_status"] = status
    c = Config.from_env({"PYPI_URL": nexus["url"] + "/repository/python/"})
    if status in {200, 204}:
        PyPIProbe(c).delete_components(["one/id"])
    else:
        message = "Overwrite denied" if status == 403 else "Overwrite failed"
        with pytest.raises(UploadError, match=message):
            PyPIProbe(c).delete_components(["one/id"])
    assert nexus["calls"][-1][1] == "/service/rest/v1/components/one%2Fid"


def test_pypi_search_pagination(nexus):
    from nexus_uploader.presence import PyPIProbe

    nexus["pages"] = [
        {"items": [{"id": "one"}], "continuationToken": "next/token"},
        {"items": [{"id": "two"}], "continuationToken": None},
    ]
    c = Config.from_env({"PYPI_URL": nexus["url"] + "/repository/python/"})
    assert PyPIProbe(c).component_ids("demo", "1") == ["one", "two"]
    query = parse_qs(urlsplit(nexus["calls"][-1][1]).query)
    assert query["continuationToken"] == ["next/token"]
    assert query["repository"] == ["python"]


@pytest.mark.parametrize("payload", [{}, {"items": [{"id": ""}]}, {"items": "secret"}])
def test_pypi_malformed_search_fails_closed(nexus, payload):
    from nexus_uploader.presence import PyPIProbe

    nexus["pages"] = [payload]
    c = Config.from_env({"PYPI_URL": nexus["url"] + "/repository/python/"})
    with pytest.raises(UploadError, match="Nexus API request failed") as caught:
        PyPIProbe(c).component_ids("demo", "1")
    assert "secret" not in str(caught.value)


def test_pypi_api_unavailable():
    from nexus_uploader.presence import PyPIProbe

    assert (
        PyPIProbe(Config.from_env({"PYPI_URL": "https://host/upload"})).component_ids("demo", "1")
        is None
    )


@pytest.mark.parametrize(
    "tls, ca", [(True, ""), (True, "/provided/ca.pem"), (False, "/provided/ca.pem")]
)
def test_probe_transport_controls(monkeypatch, tls, ca):
    from nexus_uploader.presence import PyPIProbe, RegistryProbe

    calls = []

    def request(session, method, url, **kwargs):
        assert session.trust_env is False
        assert session.timeout == 300
        assert kwargs["allow_redirects"] is False
        assert kwargs["verify"] == ((ca or True) if tls else False)
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"items": [], "continuationToken": null}'
        response._content_consumed = True
        return response

    monkeypatch.setattr("nexus_uploader.upload.TimeoutSession.request", request)
    c = replace(
        Config.from_env({"DOCKER_REGISTRY": "host", "PYPI_URL": "https://host/repository/python/"}),
        registry_tls=tls,
        pypi_tls=tls,
        ca_bundle=ca,
    )
    assert RegistryProbe(c).tag_exists("app", "v1") is True
    assert PyPIProbe(c).component_ids("app", "1") == []
    PyPIProbe(c).delete_components(["id"])
    assert calls[0][2]["auth"] is None
    assert calls[1][2]["auth"] == ("", "")
    assert calls[2][0] == "DELETE"


def test_probe_transport_errors_sanitized(monkeypatch):
    from nexus_uploader.presence import PyPIProbe, RegistryProbe

    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://user:secret@host secret")

    monkeypatch.setattr("nexus_uploader.upload.TimeoutSession.request", fail)
    c = Config.from_env({"DOCKER_REGISTRY": "host", "PYPI_URL": "https://host/repository/python/"})
    for call in [
        lambda: RegistryProbe(c).tag_exists("app", "one"),
        lambda: PyPIProbe(c).component_ids("app", "1"),
        lambda: PyPIProbe(c).delete_components(["id"]),
    ]:
        with pytest.raises(UploadError) as caught:
            call()
        assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "value, expected", [("true", True), ("false", False), ("1", True), ("0", False)]
)
def test_allow_overwrite_boolean(value, expected):
    assert Config.from_env({"ALLOW_OVERWRITE": value}).allow_overwrite is expected


def test_allow_overwrite_default_and_invalid():
    assert Config.from_env({}).allow_overwrite is False
    with pytest.raises(ValueError):
        Config.from_env({"ALLOW_OVERWRITE": "garbage"})


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host",
        "https:///path",
        "https://u:p@host",
        "https://host?q=1",
        "https://host/#f",
        "/",
        "////",
        "https://@host",
    ],
)
def test_nexus_api_url_invalid(url):
    with pytest.raises(ValueError):
        Config.from_env({"NEXUS_API_URL": url})


@pytest.mark.parametrize(
    "path, repository",
    [("/repository/pypi/", "pypi"), ("/repository/pypi/trailing/path", "pypi"), ("/upload/", "")],
)
def test_nexus_api_derivation(path, repository):
    c = Config.from_env({"PYPI_URL": "https://nexus.example" + path})
    assert c.pypi_repository == repository
    assert c.nexus_api_url == ("https://nexus.example" if repository else "")


def test_explicit_api_root_and_independent_tls():
    c = Config.from_env(
        {
            "NEXUS_API_URL": "https://api.example/nexus/",
            "TLS_VERIFY": "false",
            "PYPI_TLS_VERIFY": "true",
        }
    )
    assert c.nexus_api_url == "https://api.example/nexus"
    assert c.registry_tls is False
    assert c.pypi_tls is True
    assert replace(c, pypi_url="https://host/upload").pypi_repository == ""
