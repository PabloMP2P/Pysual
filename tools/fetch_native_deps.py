"""Fetch checksum-pinned official SDL release archives without building them.

    python tools/fetch_native_deps.py --kind windows
    python tools/fetch_native_deps.py --kind source

Archives and extracted packages are kept in .build/deps by default.
The destination should be a dependency directory, never a source checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
from urllib.parse import urlsplit
import urllib.request
import zipfile


# URLs are official SDL release assets, not GitHub's mutable source snapshots.
# SDL SHA256: github.com/libsdl-org/SDL/releases/expanded_assets/release-3.4.16
# TTF/image source digests are also recorded in the Homebrew formulas:
# raw.githubusercontent.com/Homebrew/homebrew-core/master/Formula/s/sdl3_ttf.rb
# raw.githubusercontent.com/Homebrew/homebrew-core/master/Formula/s/sdl3_image.rb
# VC archive digests pin the official development packages used by the builder.
RELEASES = (
    ("SDL", "SDL3", "3.4.16", {
        "source": "7322236cd12090c3eb40b9728be4d49c76f66ad17d04369584d4ecad5cf77c68",
        "windows": "1a784cb2a5c64d56fe7a62090fe9d242d9865f235e4ea9678f1a6ba4e693e7de",
    }),
    ("SDL_ttf", "SDL3_ttf", "3.2.2", {
        "source": "63547d58d0185c833213885b635a2c0548201cc8f301e6587c0be1a67e1e045d",
        "windows": "67805c5babfc49ca0c56882dc9b8cabbcdd1e6f9edde10ddac91ddb38f3afb8c",
    }),
    ("SDL_image", "SDL3_image", "3.4.6", {
        "source": "d2e4637ae700f72e5196b8fbd749850ed2e5e1e09c5a5be8d06ff55aaccf3b01",
        "windows": "03c6b313623edadf707a7c187e2036a5be5f12e693025c0697833379970bb4c0",
    }),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, archive: Path, expected: str) -> None:
    if archive.is_symlink() or (archive.exists() and not archive.is_file()):
        raise ValueError(f"Archive destination is not a regular file: {archive}")
    if archive.is_file() and sha256(archive) == expected:
        return
    partial = None
    request = urllib.request.Request(url, headers={"User-Agent": "Pysual-native-build"})
    try:
        # A unique, exclusively created file avoids following a pre-existing
        # .partial symlink and keeps interrupted downloads out of the cache.
        with urllib.request.urlopen(request, timeout=90) as response, tempfile.NamedTemporaryFile(
            dir=archive.parent, prefix=f".{archive.name}.", suffix=".partial", delete=False
        ) as output:
            partial = Path(output.name)
            if urlsplit(response.geturl()).scheme != "https":
                raise ValueError("Dependency download redirected away from HTTPS")
            received = 0
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > 128 * 1024 * 1024:
                    raise ValueError("Dependency archive exceeds 128 MiB")
                output.write(block)
        actual = sha256(partial)
        if actual != expected:
            raise ValueError(f"SHA256 mismatch for {archive.name}: {actual}")
        partial.replace(archive)
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


def checked_path(destination: Path, name: str, package: str) -> Path:
    pure = PurePosixPath(name)
    if (not pure.parts or pure.parts[0] != package or pure.is_absolute()
            or ".." in pure.parts or "\\" in name or ":" in name):
        raise ValueError(f"Unexpected dependency archive path: {name!r}")
    result = destination.joinpath(*pure.parts)
    if not result.resolve().is_relative_to(destination.resolve()):
        raise ValueError(f"Archive path escapes destination: {name!r}")
    return result


def extract(archive: Path, destination: Path, package: str) -> None:
    if (destination / package).is_symlink():
        raise ValueError(f"Package destination must not be a symlink: {package}")
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if sum(member.file_size for member in members) > 512 * 1024 * 1024:
                raise ValueError("Dependency archive expands beyond 512 MiB")
            # Validate all metadata before writing the first extracted file.
            for member in members:
                checked_path(destination, member.filename, package)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Unexpected symlink in Windows development archive")
            for member in members:
                target = checked_path(destination, member.filename, package)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as input_file, target.open("wb") as output_file:
                        shutil.copyfileobj(input_file, output_file)
    else:
        if not hasattr(tarfile, "data_filter"):
            raise RuntimeError("Safe source extraction requires Python 3.12+ or a Python 3.11 security update with tarfile.data_filter")
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            if sum(member.size for member in members) > 512 * 1024 * 1024:
                raise ValueError("Dependency archive expands beyond 512 MiB")
            for member in members:
                checked_path(destination, member.name, package)
            # Python's data filter additionally rejects external links, special
            # devices and unsafe permissions while retaining in-tree links.
            source.extractall(destination, members=members, filter="data")


def fetch(kind: str, destination: Path) -> list[Path]:
    if kind not in ("windows", "source"):
        raise ValueError("Dependency kind must be windows or source")
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    manifest = []
    for repository, library, version, digests in RELEASES:
        filename = (f"{library}-devel-{version}-VC.zip" if kind == "windows"
                    else f"{library}-{version}.tar.gz")
        url = f"https://github.com/libsdl-org/{repository}/releases/download/release-{version}/{filename}"
        archive = destination / filename
        package = f"{library}-{version}"
        download(url, archive, digests[kind])
        extract(archive, destination, package)
        if not (destination / package / "include" / library).is_dir():
            raise ValueError(f"Missing public headers after extracting {filename}")
        manifest.append({"library": library, "version": version, "url": url,
                         "sha256": digests[kind], "path": str(destination / package)})
        print(f"Verified {library} {version}: {destination / package}")
    # Replace the manifest atomically instead of following an existing link or
    # leaving a truncated record when interrupted during the write.
    partial_manifest = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination,
            prefix=".manifest.", suffix=".json", delete=False
        ) as output:
            partial_manifest = Path(output.name)
            output.write(json.dumps(manifest, indent=2) + "\n")
        partial_manifest.replace(destination / "manifest.json")
    finally:
        if partial_manifest is not None:
            partial_manifest.unlink(missing_ok=True)
    return [Path(item["path"]) for item in manifest]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("windows", "source"), required=True)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / ".build/deps")
    args = parser.parse_args()
    fetch(args.kind, args.destination)


if __name__ == "__main__":
    main()
