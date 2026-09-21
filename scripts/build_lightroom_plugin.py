#!/usr/bin/env python3
"""Build a self-contained Lightroom Classic plug-in on macOS.

Run from the repository: uv run --with pyinstaller python scripts/build_lightroom_plugin.py
Only the repository's build/ and dist/ directories are written.
"""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "CameraTools.lrplugin"
HELPER_NAME = "camera-tools-helper"
ZIP_EPOCH = 315532800  # 1980-01-01, the earliest ZIP timestamp.


def output_directory(root: Path, relative: str) -> Path:
    """Refuse redirected build paths before writing or replacing artifacts."""
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Build output escapes the repository: {path}")
    current = root
    for part in Path(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Build output must not be a symbolic link: {current}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def create_zip(package: Path, archive: Path, epoch: int = ZIP_EPOCH) -> None:
    """Keep executable modes and framework symlinks, with stable entry ordering."""
    timestamp = time.gmtime(max(epoch, ZIP_EPOCH))[:6]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        for path in [package, *sorted(package.rglob("*"))]:
            info = path.lstat()
            name = path.relative_to(package.parent).as_posix()
            if stat.S_ISDIR(info.st_mode):
                name += "/"
            entry = zipfile.ZipInfo(name, timestamp)
            entry.create_system = 3
            entry.external_attr = info.st_mode << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            if stat.S_ISLNK(info.st_mode):
                if not path.resolve().is_relative_to(package.resolve()):
                    raise ValueError(f"Bundle symlink escapes the plug-in: {path}")
                output.writestr(entry, os.fsencode(os.readlink(path)))
            elif stat.S_ISDIR(info.st_mode):
                entry.external_attr |= 0x10
                output.writestr(entry, b"")
            elif stat.S_ISREG(info.st_mode):
                with path.open("rb") as source, output.open(entry, "w") as target:
                    shutil.copyfileobj(source, target)
            else:
                raise ValueError(f"Unsupported bundle entry: {path}")


def pyinstaller_command(root: Path, work: Path, architecture: str) -> list[str]:
    return [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onedir", "--console", "--noupx",
        "--name", HELPER_NAME,
        "--target-architecture", architecture,
        "--distpath", str(work / "helper"),
        "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work),
        "--paths", str(root),
        "--collect-submodules", "exifread",
        "--copy-metadata", "ExifRead",
        "--exclude-module", "PySide6",
        "--exclude-module", "shiboken6",
        "--exclude-module", "PyQt6",
        "--exclude-module", "tkinter",
        "--exclude-module", "camera_tools.ui",
        str(root / "scripts" / "lightroom_helper.py"),
    ]


def copy_notices(package: Path) -> None:
    notices = package / "licenses"
    notices.mkdir()
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError(f"Python distribution license is missing: {python_license}")
    shutil.copy2(python_license, notices / "Python.txt")
    for name in ("ExifRead", "PyInstaller", "pyinstaller-hooks-contrib"):
        distribution = importlib.metadata.distribution(name)
        for file in distribution.files or ():
            if file.name.lower().startswith(("license", "copying")):
                source = distribution.locate_file(file)
                if source.is_file():
                    shutil.copy2(source, notices / f"{name}-{file.name}")


def assemble_package(root: Path, work: Path, architecture: str) -> Path:
    package = work / PLUGIN_NAME
    shutil.copytree(
        root / "lightroom" / PLUGIN_NAME, package, symlinks=True,
        ignore=shutil.ignore_patterns(".DS_Store", "__pycache__", "*.pyc", "bin"),
    )
    (package / "bin").mkdir()
    shutil.copytree(
        work / "helper" / HELPER_NAME, package / "bin" / HELPER_NAME,
        symlinks=True,
    )
    helper = package / "bin" / HELPER_NAME / HELPER_NAME
    if not helper.is_file() or not os.access(helper, os.X_OK):
        raise RuntimeError(f"Bundled helper is missing or not executable: {helper}")
    # Smoke-test without uv or a Python executable on PATH.
    environment = os.environ.copy()
    environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    subprocess.run([str(helper), "--help"], check=True, env=environment)
    copy_notices(package)
    shutil.copy2(root / "lightroom" / "README.md", package / "README.md")
    with (root / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    (package / "BUILD.txt").write_text(
        f"Camera Tools {version} for Adobe Lightroom Classic\n"
        f"Platform: macOS\nArchitecture: {architecture}\n"
        f"Build host macOS: {platform.mac_ver()[0]}\n"
        "Compatibility with older macOS versions has not been verified.\n"
        f"Python: {platform.python_version()} (bundled)\n"
        f"ExifRead: {importlib.metadata.version('ExifRead')} (bundled)\n"
        f"PyInstaller: {importlib.metadata.version('PyInstaller')}\n"
        "Bundling: onedir; Qt excluded\n"
        "Signing: ad-hoc; not notarized\n"
        "Runtime: no Python or uv installation required\n"
        "Helper: bin/camera-tools-helper/camera-tools-helper\n",
        encoding="utf-8",
    )
    return package


def main() -> int:
    if sys.platform != "darwin":
        raise RuntimeError("Build the Lightroom plug-in on macOS for the target Mac architecture.")
    architecture = platform.machine()
    if architecture not in {"arm64", "x86_64"}:
        raise RuntimeError(f"Unsupported macOS architecture: {architecture}")
    try:
        importlib.metadata.version("PyInstaller")
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeError(
            "PyInstaller is required. Run: uv run --with pyinstaller python "
            "scripts/build_lightroom_plugin.py"
        ) from error
    for source in (
        ROOT / "lightroom" / PLUGIN_NAME / "Info.lua",
        ROOT / "camera_tools" / "lightroom.py",
    ):
        if not source.is_file():
            raise RuntimeError(f"Required plug-in source is missing: {source}")

    build = output_directory(ROOT, "build/lightroom-plugin")
    destination = output_directory(ROOT, "dist")
    output = destination / PLUGIN_NAME
    archive = destination / f"CameraTools-macos-{architecture}.zip"
    for artifact in (output, archive):
        if artifact.is_symlink():
            raise ValueError(f"Refusing to replace a symbolic link: {artifact}")
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", ZIP_EPOCH))
    with tempfile.TemporaryDirectory(prefix="package-", dir=build) as temporary:
        work = Path(temporary)
        environment = os.environ.copy()
        environment.update({
            "PYINSTALLER_CONFIG_DIR": str(work / "cache"),
            "PYTHONHASHSEED": "0",
            "SOURCE_DATE_EPOCH": str(epoch),
        })
        subprocess.run(
            pyinstaller_command(ROOT, work, architecture),
            check=True, cwd=ROOT, env=environment,
        )
        package = assemble_package(ROOT, work, architecture)
        staged_archive = work / archive.name
        create_zip(package, staged_archive, epoch)
        # Replace only these named build artifacts after the build succeeds.
        if output.exists():
            shutil.rmtree(output)
        shutil.move(str(package), output)
        staged_archive.replace(archive)
    print(f"Plug-in: {output}\nInstall archive: {archive}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
