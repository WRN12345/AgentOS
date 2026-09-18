"""Exercise deployment script control flow without Docker or a real database."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database():
    pass


@pytest.fixture(autouse=True)
def _clean_tables():
    pass


@pytest.fixture
def deployment(tmp_path):
    scripts = tmp_path / "deploy" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "deploy" / "scripts"
    if not source.exists():
        pytest.skip("Deployment script tests require the repository root mount")
    for name in ("backup.sh", "restore.sh"):
        shutil.copyfile(source / name, scripts / name)
    binary = tmp_path / "bin"
    binary.mkdir()
    docker = binary / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "ps" in args:
    print("postgres")
elif "pg_dump" in args:
    sys.stdout.buffer.write(b"fake database dump")
elif any("verify_snapshot" in item for item in args):
    if os.environ.get("BAD_MANIFEST") == "1":
        sys.exit(1)
    print(os.environ.get("HAS_MINIO", "0"))
elif "export" in args:
    if os.environ.get("FAIL_EXPORT") == "1":
        sys.exit(1)
    mount = next(item for item in args if item.endswith(":/snapshot"))
    root = pathlib.Path(mount.removesuffix(":/snapshot"))
    (root / "manifest.json").write_text('{"version":1,"files":[]}')
elif "psql" in args:
    query = args[-1]
    if "count(*)" in query:
        print(os.environ.get("HAS_MINIO", "0"))
    elif "to_regclass" in query:
        print("t")
    elif "SELECT 1" in query:
        print("1")
''')
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls.jsonl"
    env = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}",
           "DOCKER_CALLS": str(calls), "LOG_DIR": str(tmp_path / "logs"),
           "BACKUP_DIR": str(tmp_path / "backups"), "POSTGRES_DB": "agentos",
           "MINIO_BUCKET": "live-bucket", "VERIFY_SAMPLE_SIZE": "20"}
    (tmp_path / "db.dump").write_bytes(b"fake dump")
    (tmp_path / "snapshot.tar.gz").write_bytes(b"fake archive")

    def run(script, *args, **overrides):
        result = subprocess.run(["bash", str(scripts / script), *args], cwd=tmp_path,
                                env={**env, **overrides}, capture_output=True, text=True)
        recorded = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, recorded

    return tmp_path, run


@pytest.mark.parametrize("case", ["live-directory", "live-bucket", "missing-bucket", "bad-manifest"])
def test_restore_refuses_unsafe_target_before_dropping_database(deployment, case):
    root, run = deployment
    args = ["--dump", "db.dump", "--target-db", "restore_test",
            "--storage-archive", "snapshot.tar.gz", "--uploads-target",
            "data/uploads" if case == "live-directory" else "restore-uploads"]
    if case == "live-bucket":
        args += ["--minio-target-bucket", "live-bucket"]
    result, calls = run("restore.sh", *args,
                        HAS_MINIO="1" if case == "missing-bucket" else "0",
                        BAD_MANIFEST="1" if case == "bad-manifest" else "0")
    assert result.returncode != 0
    assert not any("DROP DATABASE" in part for call in calls for part in call)


def test_restore_passes_isolated_database_and_storage_targets(deployment):
    _, run = deployment
    result, calls = run("restore.sh", "--dump", "db.dump", "--target-db", "restore_test",
                        "--storage-archive", "snapshot.tar.gz", "--uploads-target", "restored files",
                        "--minio-target-bucket", "restore-bucket", HAS_MINIO="1")
    assert result.returncode == 0, result.stdout + result.stderr
    restore = next(call for call in calls if "import" in call)
    assert "RESTORE_TARGET_DB=restore_test" in restore
    assert "MINIO_BUCKET=restore-bucket" in restore
    assert "--local-root" in restore and "/restore-uploads" in restore
    assert "--yes" in restore


@pytest.mark.parametrize("fail", [False, True])
def test_backup_requires_complete_storage_snapshot(deployment, fail):
    root, run = deployment
    result, calls = run("backup.sh", FAIL_EXPORT="1" if fail else "0")
    assert any("export" in call for call in calls)
    archives = list((root / "backups" / "storage").glob("*-storage.tar.gz"))
    assert bool(archives) is not fail
    assert (result.returncode != 0) is fail
    assert not list((root / "backups" / "storage").glob(".snapshot-*"))
