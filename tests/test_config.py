from backend.config import Config


def test_runtime_keeps_virtual_environment_path(tmp_path, monkeypatch):
    base_python = tmp_path / "standalone-python"
    base_python.touch()
    bin_directory = tmp_path / "venv" / "bin"
    bin_directory.mkdir(parents=True)
    venv_python = bin_directory / "python"
    venv_python.symlink_to(base_python)
    monkeypatch.setenv("DIARIZE_PYTHON", str(venv_python))

    assert Config().runtime("embed") == venv_python
