import pytest
from test_archives import archive

from nexus_uploader.archives import archive_tags
from nexus_uploader.config import Config, image_target


@pytest.mark.parametrize("source_prefix", ["old.example:5000/team", "team"])
def test_replace_registry_qualified_prefix(tmp_path, source_prefix):
    tag = "old.example:5000/team/app:v1"
    config = Config.from_env(
        {
            "IMAGE_PREFIX_MODE": "replace",
            "IMAGE_SOURCE_PREFIX": source_prefix,
            "IMAGE_PREFIX": "mirror",
        }
    )
    assert archive_tags(archive(tmp_path / "save.tar", [{"RepoTags": [tag]}])) == [tag]
    assert image_target(tag, config) == "mirror/app:v1"


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("docker.io/library/python:3.11", "mirror/library/python:3.11"),
        ("localhost:5000/team/app:v1", "mirror/team/app:v1"),
        ("registry.example/mirror/app:TAG", "mirror/app:TAG"),
    ],
)
def test_prepend_strips_source_registry_and_is_idempotent(tag, expected):
    assert image_target(tag, Config.from_env({"IMAGE_PREFIX": "mirror"})) == expected
