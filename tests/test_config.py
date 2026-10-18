import pytest

from nexus_uploader.config import Config, image_target


def test_config_and_default_preservation(tmp_path):
    c = Config.from_env({"INPUT_DIR": str(tmp_path)})
    assert c.docker_tls and c.pypi_tls
    assert image_target("team/app:one", c) == "team/app:one"


def test_prefix_boundaries_and_idempotence():
    c = Config.from_env({"IMAGE_PREFIX": "mirror"})
    assert image_target("team/app:v1", c) == "mirror/team/app:v1"
    assert image_target("mirror/team/app:v1", c) == "mirror/team/app:v1"
    c = Config.from_env(
        {"IMAGE_PREFIX_MODE": "replace", "IMAGE_PREFIX": "new", "IMAGE_SOURCE_PREFIX": "old"}
    )
    assert image_target("old/app:v1", c) == "new/app:v1"
    assert image_target("older/app:v1", c) == "older/app:v1"


@pytest.mark.parametrize(
    "env",
    [
        {"TLS_VERIFY": "nope"},
        {"POLL_INTERVAL": "0"},
        {"IMAGE_PREFIX_MODE": "bad"},
        {"DOCKER_REGISTRY": "https://host"},
        {"PYPI_URL": "https://user:pass@host/repository/pypi/"},
        {"PYPI_URL": "https://host/repository/pypi/simple/"},
        {"IMAGE_PREFIX": "../oops"},
        {"UPLOAD_RETRIES": "-1"},
    ],
)
def test_invalid_config(env):
    with pytest.raises(ValueError):
        Config.from_env(env)


def test_tls_independent():
    c = Config.from_env({"TLS_VERIFY": "false", "PYPI_TLS_VERIFY": "true"})
    assert not c.docker_tls and c.pypi_tls


def test_blank_tls_override_falls_back():
    c = Config.from_env({"TLS_VERIFY": "false", "DOCKER_TLS_VERIFY": "", "PYPI_TLS_VERIFY": ""})
    assert not c.docker_tls and not c.pypi_tls
