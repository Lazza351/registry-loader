"""Daemonless Docker copy and Twine multipart upload; errors are deliberately sanitized."""

import base64
import contextlib
import gzip
import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import requests
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
        with tempfile.TemporaryDirectory(prefix="nexus-auth-") as folder:
            source_path = path
            # Bookworm skopeo 1.9 does not decompress docker-archive gzip sources.
            # Decode once for all tags and keep image-sized temporary data on TMPDIR.
            with path.open("rb") as probe:
                compressed = probe.read(2) == b"\x1f\x8b"
            if compressed:
                source_path = Path(folder) / "archive.tar"
                with gzip.open(path, "rb") as src, source_path.open("xb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            auth = Path(folder) / "auth.json"
            credentials = base64.b64encode(f"{c.docker_user}:{c.docker_password}".encode()).decode()
            fd = os.open(auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump({"auths": {c.registry: {"auth": credentials}}}, stream)
            if c.ca_bundle:
                shutil.copyfile(c.ca_bundle, Path(folder) / "ca.crt")
            for tag, destination in zip(tags, destinations):
                started = time.monotonic()
                result = {
                    "kind": "docker",
                    "tag": tag,
                    "destination": f"{c.registry}/{destination}",
                    "status": "error",
                }
                argv = [
                    c.skopeo,
                    "--tmpdir",
                    folder,
                    "copy",
                    "--authfile",
                    str(auth),
                    f"--dest-tls-verify={str(c.docker_tls).lower()}",
                ]
                if c.ca_bundle:
                    argv += ["--dest-cert-dir", folder]
                argv += [
                    f"docker-archive:{source_path}:{tag}",
                    f"docker://{c.registry}/{destination}",
                ]
                for attempt in range(c.retries + 1):
                    try:
                        subprocess.run(
                            argv,
                            check=True,
                            timeout=c.timeout,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        result["status"] = "success"
                        break
                    except (OSError, subprocess.SubprocessError):
                        if attempt < c.retries:
                            time.sleep(min(2**attempt, 8))
                result["duration"] = round(time.monotonic() - started, 3)
                if result["status"] == "error":
                    result["error"] = "Docker upload failed (remote output suppressed)"
                results.append(result)
                log.info("tag_result %s", json.dumps(result, ensure_ascii=True))
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
