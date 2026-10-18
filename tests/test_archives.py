import io
import json
import tarfile

import pytest

from nexus_uploader.archives import archive_tags, classify


def archive(path, manifest):
    with tarfile.open(path, "w:gz" if str(path).endswith(".gz") else "w") as tf:
        data = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize("suffix", [".tar", ".tar.gz"])
def test_multiple_images_tags(tmp_path, suffix):
    p = archive(
        tmp_path / ("save" + suffix),
        [{"RepoTags": ["team/app:v1", "team/app:latest"]}, {"RepoTags": ["other:v2"]}],
    )
    assert archive_tags(p) == ["team/app:v1", "team/app:latest", "other:v2"]
    assert classify(p) == ("tar-gzip" if suffix == ".tar.gz" else "docker")


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        {},
        [{"RepoTags": None}],
        [{"RepoTags": ["no-tag"]}],
        [{"RepoTags": ["a:v", "a:v"]}],
        [{"RepoTags": ["a:v"]}, {"RepoTags": ["a:v"]}],
        [{"RepoTags": ["../bad:v"]}],
        [{"RepoTags": ["a:v"], "Layers": "bad"}],
    ],
)
def test_reject_ambiguous_archives(tmp_path, manifest):
    with pytest.raises(ValueError):
        archive_tags(archive(tmp_path / "bad.tar", manifest))


def test_registry_qualified_source(tmp_path):
    from nexus_uploader.config import Config, image_target

    tag = "source.example:5000/team/app:v1"
    assert archive_tags(archive(tmp_path / "a.tar", [{"RepoTags": [tag]}])) == [tag]
    assert image_target(tag, Config.from_env({})) == "team/app:v1"


def test_never_extract_symlink(tmp_path):
    p = tmp_path / "bad.tar"
    with tarfile.open(p, "w") as tf:
        info = tarfile.TarInfo("manifest.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ValueError):
        archive_tags(p)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("pkg.whl", "pypi"),
        ("pkg-1.zip", "pypi"),
        ("pkg-1.tar.gz", "tar-gzip"),
        ("image.tar.gz", "tar-gzip"),
        ("x.part", None),
        ("x.whl.tmp", None),
        ("unknown.txt", None),
    ],
)
def test_classification(name, expected):
    assert classify(name) == expected
