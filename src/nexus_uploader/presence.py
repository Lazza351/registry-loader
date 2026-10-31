"""Registry and Nexus existence probes with explicit, sanitized HTTP transport."""

import re
from urllib.parse import quote

import requests

from .upload import TimeoutSession, UploadError


class RegistryProbe:
    def __init__(self, config):
        self.config = config

    def tag_exists(self, repository, tag):
        c = self.config
        scheme = "https" if c.registry_tls else "http"
        url = (
            f"{scheme}://{c.registry}/v2/{quote(repository, safe='/')}"
            f"/manifests/{quote(tag, safe='')}"
        )
        accept = (
            "application/vnd.docker.distribution.manifest.v2+json, "
            "application/vnd.oci.image.manifest.v1+json, "
            "application/vnd.docker.distribution.manifest.list.v2+json, "
            "application/vnd.oci.image.index.v1+json"
        )
        try:
            with (
                TimeoutSession(c.timeout) as session,
                session.get(
                    url,
                    headers={"Accept": accept},
                    auth=(c.docker_user, c.docker_password) if c.docker_user else None,
                    verify=(c.ca_bundle or True) if c.registry_tls else False,
                    allow_redirects=False,
                ) as response,
            ):
                status = response.status_code
        except requests.RequestException:
            raise UploadError("Registry existence check failed (network error)") from None
        if 200 <= status < 300:
            return True
        if status == 404:
            return False
        if status in {401, 403}:
            raise UploadError("Registry authentication failed during existence check")
        raise UploadError(f"Registry existence check failed (HTTP {status})")


class PyPIProbe:
    def __init__(self, config):
        self.config = config

    def component_ids(self, name, version):
        c = self.config
        if not c.nexus_api_url or not c.pypi_repository:
            return None
        params = {
            "repository": c.pypi_repository,
            "name": re.sub(r"[-_.]+", "-", name).lower(),
            "version": version,
        }
        ids, tokens = [], set()
        try:
            with TimeoutSession(c.timeout) as session:
                while True:
                    with session.get(
                        c.nexus_api_url + "/service/rest/v1/search",
                        params=params,
                        auth=(c.pypi_user, c.pypi_password),
                        verify=(c.ca_bundle or True) if c.pypi_tls else False,
                        allow_redirects=False,
                    ) as response:
                        status = response.status_code
                        if status == 404:
                            return ids
                        if not 200 <= status < 300:
                            raise UploadError(f"Nexus API request failed (HTTP {status})")
                        data = response.json()
                    items = data["items"]
                    if not isinstance(items, list):
                        raise TypeError
                    for item in items:
                        component_id = item["id"]
                        if not isinstance(component_id, str) or not component_id:
                            raise ValueError
                        if component_id not in ids:
                            ids.append(component_id)
                    token = data.get("continuationToken")
                    if token is None:
                        return ids
                    if not isinstance(token, str) or not token or token in tokens:
                        raise ValueError
                    tokens.add(token)
                    params["continuationToken"] = token
        except requests.RequestException:
            raise UploadError("Nexus API request failed (network error)") from None
        except (ValueError, KeyError, TypeError):
            raise UploadError("Nexus API request failed (invalid response)") from None

    def delete_components(self, ids):
        c = self.config
        try:
            with TimeoutSession(c.timeout) as session:
                for component_id in ids:
                    with session.delete(
                        c.nexus_api_url
                        + "/service/rest/v1/components/"
                        + quote(component_id, safe=""),
                        auth=(c.pypi_user, c.pypi_password),
                        verify=(c.ca_bundle or True) if c.pypi_tls else False,
                        allow_redirects=False,
                    ) as response:
                        status = response.status_code
                    if status in {401, 403}:
                        raise UploadError(
                            "Overwrite denied: credentials cannot delete existing artifacts"
                        )
                    if not 200 <= status < 300:
                        raise UploadError(f"Overwrite failed (HTTP {status})")
        except requests.RequestException:
            raise UploadError("Overwrite failed (network error)") from None
