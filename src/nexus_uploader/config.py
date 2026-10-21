"""Validated, explicit configuration. No ambient credential discovery."""

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .archives import REPOSITORY, SOURCE_NAME


def boolean(value):
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError("Boolean must be true/false/1/0")
    return value.lower() in {"true", "1"}


@dataclass(frozen=True)
class Config:
    input_dir: Path
    processing_dir: Path
    success_dir: Path
    error_dir: Path
    registry: str = ""
    pypi_url: str = ""
    docker_user: str = field(default="", repr=False)
    docker_password: str = field(default="", repr=False)
    pypi_user: str = field(default="", repr=False)
    pypi_password: str = field(default="", repr=False)
    pypi_tls: bool = True
    ca_bundle: str = ""
    prefix: str = ""
    source_prefix: str = ""
    prefix_mode: str = "prepend"
    poll: float = 2
    stable: float = 10
    timeout: float = 300
    retries: int = 2
    extra_input_dirs: tuple[Path, ...] = ()

    @property
    def watch_dirs(self):
        return (self.input_dir, *self.extra_input_dirs)

    @classmethod
    def from_env(cls, env):
        root = Path(env.get("INPUT_DIR", "./data/input")).resolve()
        inputs = [root]
        if env.get("INPUT_DIRS", "").strip():
            entries = [v.strip() for v in env["INPUT_DIRS"].split(",")]
            if not all(entries):
                raise ValueError("INPUT_DIRS contains an empty path")
            inputs = [Path(v).resolve() for v in entries]
            root = inputs[0]
        paths = [root] + [
            Path(env.get(k, f"./data/{v}")).resolve()
            for k, v in [
                ("PROCESSING_DIR", "processing"),
                ("SUCCESS_DIR", "success"),
                ("ERROR_DIR", "error"),
            ]
        ]
        all_paths = [*paths, *inputs[1:]]
        if any(
            a == b or a in b.parents or b in a.parents
            for i, a in enumerate(all_paths)
            for b in all_paths[i + 1 :]
        ):
            raise ValueError("Data directories must be separate, not nested")
        registry = env.get("DOCKER_REGISTRY", "").strip()
        if registry and not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", registry):
            raise ValueError("DOCKER_REGISTRY must be host[:port], without scheme/path")
        url = env.get("PYPI_URL", "")
        if url:
            u = urlsplit(url)
            if (
                u.scheme not in {"https", "http"}
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
                or "/simple" in u.path
            ):
                raise ValueError("PYPI_URL must be a hosted upload URL, without credentials")
        mode = env.get("IMAGE_PREFIX_MODE", "prepend")
        if mode not in {"prepend", "replace"}:
            raise ValueError("Invalid IMAGE_PREFIX_MODE")
        prefix, source = [
            env.get(k, "").strip("/") for k in ["IMAGE_PREFIX", "IMAGE_SOURCE_PREFIX"]
        ]
        for value, pattern in ((prefix, REPOSITORY), (source, SOURCE_NAME)):
            if value and not re.fullmatch(pattern, value):
                raise ValueError("Invalid image prefix")
        if mode == "replace" and not source:
            raise ValueError("replace requires IMAGE_SOURCE_PREFIX")
        tls = env.get("TLS_VERIFY", "true")
        numbers = [
            float(env.get(k, default))
            for k, default in [
                ("POLL_INTERVAL", "2"),
                ("STABLE_SECONDS", "10"),
                ("UPLOAD_TIMEOUT", "300"),
            ]
        ]
        import math

        if any(not math.isfinite(n) or n <= 0 for n in numbers):
            raise ValueError("Intervals and timeout must be positive finite numbers")
        retries = int(env.get("UPLOAD_RETRIES", "2"))
        if not 0 <= retries <= 10:
            raise ValueError("UPLOAD_RETRIES must be 0..10")
        ca = env.get("CA_BUNDLE", "")
        if ca and not Path(ca).is_file():
            raise ValueError("CA_BUNDLE does not exist")
        return cls(
            *paths,
            registry=registry,
            pypi_url=url,
            docker_user=env.get("DOCKER_USERNAME", ""),
            docker_password=env.get("DOCKER_PASSWORD", ""),
            pypi_user=env.get("PYPI_USERNAME", ""),
            pypi_password=env.get("PYPI_PASSWORD", ""),
            pypi_tls=boolean(env.get("PYPI_TLS_VERIFY") or tls),
            ca_bundle=ca,
            prefix=prefix,
            source_prefix=source,
            prefix_mode=mode,
            poll=numbers[0],
            stable=numbers[1],
            timeout=numbers[2],
            retries=retries,
            extra_input_dirs=tuple(inputs[1:]),
        )


def image_target(tag, config):
    name, version = tag.rsplit(":", 1)
    original = name
    first, separator, remainder = name.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        name = remainder
    p, s = config.prefix, config.source_prefix
    if p and (name == p or name.startswith(p + "/")):
        return name + ":" + version
    if config.prefix_mode == "prepend" and p:
        name = p + "/" + name
    elif config.prefix_mode == "replace":
        for candidate in (original, name):
            if candidate == s or candidate.startswith(s + "/"):
                name = "/".join(x for x in [p, candidate[len(s) :].lstrip("/")] if x)
                break
    if not re.fullmatch(REPOSITORY, name):
        raise ValueError("Prefix produces invalid image repository")
    return name + ":" + version
