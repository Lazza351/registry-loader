"""Verify payload integrity and read Docker manifests without extracting archive paths."""

import gzip
import json
import re
import tarfile
import zipfile
from pathlib import Path

MAX_MANIFEST = 4 * 1024 * 1024
COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
REPOSITORY = rf"{COMPONENT}(?:/{COMPONENT})*"
SOURCE_NAME = rf"(?:[a-z0-9][a-z0-9.-]*:[0-9]+/)?{REPOSITORY}"
TAG = re.compile(rf"{SOURCE_NAME}:[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}}")


def classify(path):
    name = Path(path).name
    if name.endswith(".tar.gz"):
        return "tar-gzip"
    if name.endswith(".tar"):
        return "docker"
    if name.endswith((".whl", ".zip")):
        return "pypi"
    return None


def verify_payload(path, kind):
    """Read container integrity before handing bytes to any upload backend."""
    if kind == "docker":
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            if stream.tell() < 1024:
                raise ValueError("Missing tar terminator")
            stream.seek(-1024, 2)
            if stream.read() != b"\0" * 1024:
                raise ValueError("Missing tar terminator")
        with tarfile.open(path, "r|") as archive:
            for member in archive:
                pass
    elif kind == "tar-gzip":
        with gzip.open(path, "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    pass
            # Tar stops at its zero blocks, before gzip necessarily checks CRC/ISIZE.
            while stream.read(1024 * 1024):
                pass
    elif kind == "pypi":
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Bad ZIP member")


def read_manifest(path):
    found = None
    with tarfile.open(path, "r|*") as tf:
        for member in tf:
            if member.name in {"manifest.json", "./manifest.json"}:
                if found is not None or not member.isfile() or member.size > MAX_MANIFEST:
                    raise ValueError("Invalid or duplicate Docker manifest")
                with tf.extractfile(member) as stream:
                    found = json.loads(stream.read(MAX_MANIFEST + 1))
    return found


def archive_tags(path):
    manifest = read_manifest(path)
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("Missing Docker manifest; expected docker save archive")
    tags = []
    for image in manifest:
        if (
            not isinstance(image, dict)
            or not isinstance(image.get("RepoTags"), list)
            or not image["RepoTags"]
        ):
            raise ValueError("Untagged Docker image is not supported")
        if "Layers" in image and not isinstance(image["Layers"], list):
            raise ValueError("Invalid Docker layers")
        for tag in image["RepoTags"]:
            if not isinstance(tag, str) or not TAG.fullmatch(tag) or tag in tags:
                raise ValueError("Invalid or ambiguous Docker source tag")
            tags.append(tag)
    return tags
