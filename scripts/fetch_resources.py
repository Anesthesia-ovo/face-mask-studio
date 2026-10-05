"""Build-time downloader. Never imported, executed or bundled by the offline app.

Resources are pinned to immutable URLs and SHA256 in resources/manifest.json.
Only one named ffmpeg.exe member is copied from the verified ZIP; no archive
member is extracted using an archive-supplied filesystem path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "resources" / "manifest.json"
MAX_DOWNLOAD_BYTES = 384 * 1024 * 1024


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def target_path(relative: str) -> Path:
    parts = PurePosixPath(relative)
    if parts.is_absolute() or not parts.parts or parts.parts[0] not in {"models", "assets"}:
        raise ValueError("Invalid artifact path: " + relative)
    if any(part in {".", ".."} or "\\" in part or ":" in part for part in parts.parts):
        raise ValueError("Unsafe artifact path: " + relative)
    target = ROOT.joinpath(*parts.parts).resolve()
    if not target.is_relative_to(ROOT):
        raise ValueError("Artifact path leaves the repository: " + relative)
    return target


def verified(path: Path, expected: str, size=None) -> bool:
    return bool(path.is_file() and (size is None or path.stat().st_size == size) and digest(path) == expected)


class HTTPSRedirectsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlparse(new_url).scheme != "https":
            raise RuntimeError("Refusing a non-HTTPS download redirect.")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def download(url: str, destination: Path, expected: str, size=None) -> None:
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("Only HTTPS build resources are accepted.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part-" + uuid.uuid4().hex)
    request = urllib.request.Request(url, headers={"User-Agent": "FaceMaskStudio-build-resources/1.0"})
    opener = urllib.request.build_opener(HTTPSRedirectsOnly())
    total, last_notice = 0, 0
    try:
        print("Downloading " + destination.name, flush=True)
        with opener.open(request, timeout=90) as response, temporary.open("xb") as output:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES or (size is not None and total > size):
                    raise RuntimeError("Resource download exceeds the allowed size.")
                output.write(chunk)
                if total - last_notice >= 16 * 1024 * 1024:
                    print("  " + str(round(total / (1024 * 1024))) + " MiB downloaded", flush=True)
                    last_notice = total
        if not verified(temporary, expected, size):
            raise RuntimeError("Resource SHA256/size mismatch: " + destination.name)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def extract_ffmpeg(archive: Path, artifact: dict, destination: Path) -> None:
    member = artifact["archive_member"]
    parts = PurePosixPath(member)
    if parts.is_absolute() or ".." in parts.parts or "\\" in member or ":" in member:
        raise ValueError("Unsafe pinned archive member.")
    temporary = destination.with_name(destination.name + ".part-" + uuid.uuid4().hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as package:
            info = package.getinfo(member)
            if info.is_dir() or info.file_size != artifact["bytes"]:
                raise RuntimeError("Unexpected FFmpeg archive member size.")
            with package.open(info) as source, temporary.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        if not verified(temporary, artifact["sha256"], artifact["bytes"]):
            raise RuntimeError("Extracted FFmpeg executable SHA256 mismatch.")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true", help="Check local assets without making any network request.")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1:
        raise RuntimeError("Unsupported resource manifest schema.")
    for artifact in manifest["artifacts"]:
        destination = target_path(artifact["path"])
        if verified(destination, artifact["sha256"], artifact["bytes"]):
            print("Verified " + artifact["path"], flush=True)
            continue
        if args.verify_only:
            raise RuntimeError("Missing or changed resource: " + artifact["path"])
        if "archive_url" in artifact:
            cache = ROOT / ".build-cache" / Path(urllib.parse.urlparse(artifact["archive_url"]).path).name
            if not verified(cache, artifact["archive_sha256"]):
                download(artifact["archive_url"], cache, artifact["archive_sha256"])
            extract_ffmpeg(cache, artifact, destination)
        else:
            download(artifact["url"], destination, artifact["sha256"], artifact["bytes"])
        print("Verified " + artifact["path"], flush=True)
    print("All build resources match the pinned hashes. The application itself never downloads resources.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print("Resource preparation failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
