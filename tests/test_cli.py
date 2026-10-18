import os
import subprocess
import sys


def test_cli_once_processes_stable_files_and_reports_errors(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "INPUT_DIR": str(tmp_path / "in"),
        "PROCESSING_DIR": str(tmp_path / "work"),
        "SUCCESS_DIR": str(tmp_path / "ok"),
        "ERROR_DIR": str(tmp_path / "err"),
        "STABLE_SECONDS": "0.01",
        "POLL_INTERVAL": "0.01",
    }
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "bad.whl").write_bytes(b"not a wheel")
    result = subprocess.run(
        [sys.executable, "-m", "nexus_uploader", "--once"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert len(list((tmp_path / "err").glob("*/bad.whl"))) == 1
    assert "file_result" in result.stderr


def test_cli_config_error_has_no_secrets(tmp_path):
    env = {"PATH": os.environ["PATH"], "DOCKER_REGISTRY": "https://user:SECRET@host"}
    result = subprocess.run(
        [sys.executable, "-m", "nexus_uploader", "--check-config"],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "SECRET" not in result.stderr


def test_cli_termination_graceful(tmp_path):
    import signal
    import time

    env = {
        "PATH": os.environ["PATH"],
        "INPUT_DIR": str(tmp_path / "in"),
        "PROCESSING_DIR": str(tmp_path / "work"),
        "SUCCESS_DIR": str(tmp_path / "ok"),
        "ERROR_DIR": str(tmp_path / "err"),
        "POLL_INTERVAL": "0.02",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "nexus_uploader"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(100):
            if (tmp_path / "in" / ".nexus-uploader.lock").exists():
                break
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        _, err = process.communicate(timeout=10)
        assert process.returncode == 0, err
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
