"""Data paths are independent of CWD; all writes use temporary directories."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import config
from app.core import logging as app_logging
from app.infrastructure.storage import provider as factories


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database():
    pass


@pytest.fixture(autouse=True)
def _clean_tables():
    pass


@pytest.fixture(autouse=True)
def path_environment(monkeypatch):
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.delenv("STORAGE_ROOT", raising=False)
    monkeypatch.setenv("STORAGE_BACKEND", "local")


@pytest.fixture
def checkout(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / "pyproject.toml").touch()
    (root / "docker-compose.yml").touch()
    monkeypatch.setattr(config, "__file__", str(backend / "app/core/config.py"))
    return root


@pytest.mark.parametrize("cwd", ["root", "backend", "elsewhere"])
def test_checkout_defaults_ignore_cwd(monkeypatch, tmp_path, checkout, cwd):
    root = checkout
    monkeypatch.chdir({"root": root, "backend": root / "backend", "elsewhere": tmp_path}[cwd])
    settings = config.Settings(_env_file=None)
    assert settings.log_dir == str(root / "data/logs")
    assert settings.storage_root == str(root / "data/uploads")


@pytest.mark.parametrize("layout", ["checkout", "container", "installed"])
def test_supported_layouts_and_absolute_paths(monkeypatch, tmp_path, layout):
    root = tmp_path / "deployment"
    package_parent = root / {
        "checkout": "backend",
        "container": "",
        "installed": "venv/lib/site-packages",
    }[layout]
    package_parent.mkdir(parents=True)
    if layout != "installed":
        (package_parent / "pyproject.toml").touch()
    if layout == "checkout":
        (root / "docker-compose.yml").touch()
    monkeypatch.setattr(config, "__file__", str(package_parent / "app/core/config.py"))
    monkeypatch.chdir(tmp_path)

    settings = config.Settings(
        _env_file=None, log_dir="/app/data/logs", storage_root="/app/data/uploads"
    )
    assert settings.log_dir == "/app/data/logs"
    assert settings.storage_root == "/app/data/uploads"
    if layout == "installed":
        with pytest.raises(ValidationError, match="require absolute"):
            config.Settings(_env_file=None)
    else:
        settings = config.Settings(_env_file=None)
        assert settings.log_dir == str(root / "data/logs")
        assert settings.storage_root == str(root / "data/uploads")
    assert not (root / "data").exists()


@pytest.mark.parametrize("cwd", ["root", "backend", "elsewhere"])
@pytest.mark.parametrize("explicit_log_dir", [None, "relative", "absolute"])
async def test_temporary_writes_ignore_cwd(monkeypatch, tmp_path, checkout, cwd, explicit_log_dir):
    root = checkout
    backend = root / "backend"
    monkeypatch.chdir({"root": root, "backend": backend, "elsewhere": tmp_path}[cwd])
    monkeypatch.setenv("LOG_DIR", "data/logs")
    monkeypatch.setenv("STORAGE_ROOT", "data/uploads")
    settings = config.Settings(_env_file=None)
    monkeypatch.setattr(config, "settings", settings)
    monkeypatch.setattr(factories, "settings", settings)
    monkeypatch.setattr(factories, "_provider", None)

    directory, expected = {
        None: (None, root / "data/logs"),
        "relative": ("custom/logs", root / "custom/logs"),
        "absolute": (str(tmp_path / "absolute-logs"), tmp_path / "absolute-logs"),
    }[explicit_log_dir]
    name = f"data-path-{tmp_path.name}"
    logger = app_logging.setup_logging(name, log_dir=directory)
    try:
        logger.info("temporary test log")
        assert "temporary test log" in (expected / f"{name}.log").read_text()
        # Reusing a logger must not create an unused directory.
        assert app_logging.setup_logging(name, log_dir="unused/logs") is logger
        assert not (root / "unused").exists()

        storage = factories.get_storage_provider()
        await storage.save("project/file", b"temporary upload")
        assert (root / "data/uploads/project/file").read_bytes() == b"temporary upload"
        assert await storage.load("project/file") == b"temporary upload"
        assert not (backend / "data").exists()
        assert not (backend / "backend").exists()
    finally:
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
        app_logging._initialized.discard(name)


def test_import_time_logs_are_isolated():
    directory = Path(config.settings.log_dir)
    assert directory.parent.name.startswith("agentos-tests-")
    handlers = logging.getLogger("backend").handlers
    files = [
        Path(handler.baseFilename) for handler in handlers
        if isinstance(handler, logging.FileHandler)
    ]
    assert files
    assert all(path.parent == directory for path in files)
