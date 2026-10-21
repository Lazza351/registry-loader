"""Docker Engine image loading/push and Twine multipart upload; errors are sanitized."""

import contextlib
import io
import json
import logging
import time

import docker
import requests
from docker.errors import DockerException
from requests.adapters import HTTPAdapter
from twine.package import PackageFile
from twine.repository import Repository

from .archives import archive_tags, classify, read_manifest
from .config import image_target

log = logging.getLogger(__name__)


class UploadError(Exception):
    def __init__(self, message, results=None):
        super().__init__(message)
        self.results = results or []


class TimeoutSession(requests.Session):
    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout
        self.trust_env = False  # no .netrc, proxy credentials or ambient CA overrides
        self.mount("http://", HTTPAdapter(max_retries=0))
        self.mount("https://", HTTPAdapter(max_retries=0))

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", (min(10, self.timeout), self.timeout))
        return super().request(*args, **kwargs)


class Uploader:
    def __init__(self, config):
        self.config = config

    def upload(self, path):
        kind = classify(path)
        try:
            if kind == "tar-gzip":
                kind = "docker" if read_manifest(path) is not None else "pypi"
            if kind == "docker":
                return self._docker(path)
            if kind == "pypi":
                return self._pypi(path)
            raise UploadError("Unsupported file type")
        except UploadError:
            raise
        except Exception:  # noqa: BLE001 -- sanitize third-party parser exceptions at the boundary.
            raise UploadError("Invalid archive or package metadata") from None

    def _docker(self, path):
        c = self.config
        if not c.registry:
            raise UploadError("DOCKER_REGISTRY is not configured")
        tags = archive_tags(path)
        destinations = [image_target(t, c) for t in tags]
        if len(set(destinations)) != len(destinations):
            raise UploadError("Image prefix maps multiple source tags to the same destination")
        results = []
        client = None
        try:
            # Use the local socket explicitly; DOCKER_TLS_VERIFY is for the registry.
            client = docker.DockerClient(
                base_url="unix:///var/run/docker.sock", timeout=c.timeout, version="auto"
            )
            with path.open("rb") as archive:
                client.images.load(archive)
            auth = {"username": c.docker_user, "password": c.docker_password}
            for tag, destination in zip(tags, destinations):
                started = time.monotonic()
                result = {
                    "kind": "docker",
                    "tag": tag,
                    "destination": f"{c.registry}/{destination}",
                    "status": "error",
                }
                repository, target_tag = destination.rsplit(":", 1)
                target = f"{c.registry}/{repository}"
                try:
                    image = client.images.get(tag)
                    if not image.tag(target, tag=target_tag, force=True):
                        raise DockerException("Image retag failed")
                    for attempt in range(c.retries + 1):
                        try:
                            stream = client.api.push(
                                target, tag=target_tag, auth_config=auth, stream=True, decode=True
                            )
                            for event in stream:
                                if event.get("error") or event.get("errorDetail"):
                                    raise DockerException("Registry push failed")
                            result["status"] = "success"
                            break
                        except DockerException:
                            if attempt == c.retries:
                                break
                            time.sleep(min(2**attempt, 8))
                except DockerException:
                    pass
                result["duration"] = round(time.monotonic() - started, 3)
                if result["status"] == "error":
                    result["error"] = "Docker upload failed (remote output suppressed)"
                results.append(result)
                log.info("tag_result %s", json.dumps(result, ensure_ascii=True))
        except DockerException:
            raise UploadError("Docker daemon load failed") from None
        finally:
            if client is not None:
                client.close()
        if any(r["status"] == "error" for r in results):
            raise UploadError("Docker upload failed; see per-tag results", results)
        return results

    def _pypi(self, path):
        c = self.config
        if not c.pypi_url:
            raise UploadError("PYPI_URL is not configured")
        package = PackageFile.from_filename(str(path), None)
        # Twine is used only for metadata/multipart. Never run keyring or read .pypirc.
        repository = Repository(c.pypi_url, None, None, disable_progress_bar=True)
        repository.session.close()
        repository.session = TimeoutSession(c.timeout)
        repository.session.auth = (c.pypi_user, c.pypi_password)
        repository.session.verify = (c.ca_bundle or True) if c.pypi_tls else False
        started = time.monotonic()
        try:
            for attempt in range(c.retries + 1):
                try:
                    # _upload prints package names; suppress to preserve structured logs.
                    with contextlib.redirect_stdout(io.StringIO()):
                        # Pinned Twine 6.x single-attempt seam: public upload logs untrusted
                        # response.reason and adds implicit retries. Own retries stay sanitized.
                        response = repository._upload(package)
                    status = response.status_code
                    response.close()
                    if 200 <= status < 300:
                        return [
                            {
                                "kind": "pypi",
                                "status": "success",
                                "duration": round(time.monotonic() - started, 3),
                            }
                        ]
                    if status not in {429, 500, 502, 503, 504}:
                        raise UploadError(f"PyPI upload rejected (HTTP {status})")
                except requests.RequestException:
                    pass
                if attempt < c.retries:
                    time.sleep(min(2**attempt, 8))
            raise UploadError("PyPI upload failed (network error or retry limit)")
        finally:
            repository.close()
