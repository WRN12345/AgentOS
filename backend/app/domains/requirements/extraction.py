"""Bounded extraction of untrusted project documents."""

import asyncio
import io
import sys
import zipfile
from pathlib import PurePosixPath

MAX_TEXT = 100000
EXTRACTION_TIMEOUT = 15
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


async def extract_material_text(filename: str, data: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in {".md", ".txt"}:
        if len(data) > MAX_TEXT * 4:
            raise ValueError("Material text exceeds limit")
        result = data.decode("utf-8", errors="replace")
    else:
        if suffix == ".docx":
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_ARCHIVE_BYTES:
                    raise ValueError("Expanded document exceeds limit")
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.domains.requirements.extraction", filename,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(data), EXTRACTION_TIMEOUT)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise ValueError("Document extraction failed or exceeded resource limits")
        result = output.decode("utf-8")
    if not result.strip() or len(result) > MAX_TEXT:
        raise ValueError("Material must contain 1 to 100000 characters of text")
    return result


def main() -> None:
    # Parser allocation and CPU limits apply before importing document libraries.
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    from app.domains.memory.extractors import extract_text

    result = extract_text(sys.argv[1], sys.stdin.buffer.read())
    if not result.strip() or len(result) > MAX_TEXT:
        raise ValueError("Material text exceeds limit")
    sys.stdout.buffer.write(result.encode("utf-8"))


if __name__ == "__main__":
    main()
