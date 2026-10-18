"""Read Docker manifest without extracting untrusted archive paths."""

import json
import re
import tarfile
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
