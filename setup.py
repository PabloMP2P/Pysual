"""Only native-containing wheels receive a platform tag; the core is pure Python."""
from pathlib import Path
import re
import shutil
import subprocess
import sys

from setuptools import Distribution, setup
from setuptools.command.bdist_wheel import bdist_wheel
from setuptools.command.build_py import build_py


NATIVE = Path(__file__).parent / "src" / "pysual" / "bin" / ("pysual-host.exe" if sys.platform == "win32" else "pysual-host")
HAS_NATIVE = NATIVE.is_file()


def macos_native_platform(executable, *, runner=None):
    """Read the host's Mach-O slices and deployment target, never Python's tag."""
    run = subprocess.check_output if runner is None else runner

    def inspect(*arguments):
        try:
            return run(list(arguments), text=True, stderr=subprocess.STDOUT, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(
                f"Cannot inspect native macOS host {executable}: {' '.join(arguments[:2])} failed. "
                "Install Xcode command-line tools and rebuild the native host before packaging."
            ) from error

    architectures = set(inspect("lipo", "-archs", str(executable)).split())
    suffixes = {frozenset({"arm64"}): "arm64", frozenset({"x86_64"}): "x86_64",
                frozenset({"arm64", "x86_64"}): "universal2"}
    suffix = suffixes.get(frozenset(architectures))
    if suffix is None:
        raise RuntimeError(f"Unsupported or unknown native macOS architectures: {sorted(architectures)}")
    minimums = []
    for architecture in sorted(architectures):
        output = inspect("otool", "-arch", architecture, "-l", str(executable))
        versions = []
        for block in re.split(r"(?m)^\s*Load command \d+\s*$", output):
            if re.search(r"(?m)^\s*cmd\s+LC_BUILD_VERSION\s*$", block):
                platform = re.search(r"(?m)^\s*platform\s+(\S+)\s*$", block)
                if platform is None or platform.group(1).upper() not in {"1", "MACOS", "MACOSX"}:
                    raise RuntimeError(f"Native {architecture} slice does not declare the macOS platform")
                field = "minos"
            elif re.search(r"(?m)^\s*cmd\s+LC_VERSION_MIN_MACOSX\s*$", block):
                field = "version"
            else:
                continue
            match = re.search(rf"(?m)^\s*{field}\s+(\d+)\.(\d+)(?:\.(\d+))?\s*$", block)
            if match is None:
                raise RuntimeError(f"Cannot read the minimum macOS version of the native {architecture} slice")
            version = tuple(int(part or 0) for part in match.groups())
            if version[0] < 10 or version[2] or (version[0] >= 11 and version[1]):
                raise RuntimeError(
                    f"Native {architecture} minimum macOS {'.'.join(map(str, version))} cannot be represented "
                    "accurately by a wheel tag; rebuild with MACOSX_DEPLOYMENT_TARGET set to a "
                    "whole-major target for macOS 11 or newer (for example 11.0 or 26.0), without a patch version."
                )
            versions.append(version[:2])
        if not versions:
            raise RuntimeError(f"Native {architecture} slice has no macOS minimum-version load command")
        minimums.append(max(versions))
    major, minor = max(minimums)
    return f"macosx_{major}_{minor}_{suffix}"


class PysualDistribution(Distribution):
    def has_ext_modules(self):
        return HAS_NATIVE


class PysualWheel(bdist_wheel):
    def get_tag(self):
        if HAS_NATIVE and sys.platform == "darwin":
            return "py3", "none", macos_native_platform(NATIVE)
        python, abi, platform = super().get_tag()
        return ("py3", "none", platform) if HAS_NATIVE else (python, abi, platform)


class PysualBuild(build_py):
    def find_data_files(self, package, src_dir):
        files = super().find_data_files(package, src_dir)
        if package == "pysual" and not HAS_NATIVE:
            native_dir = (Path(src_dir) / "bin").resolve()
            files = [file for file in files if not Path(file).resolve().is_relative_to(native_dir)]
        return files

    def run(self):
        # Repeated builds must not retain deleted modules, data, or native files.
        destination = (Path(self.build_lib) / "pysual").resolve()
        if destination.is_relative_to(Path(self.build_lib).resolve()) and destination.is_dir():
            shutil.rmtree(destination)
        super().run()


setup(distclass=PysualDistribution, cmdclass={"bdist_wheel": PysualWheel, "build_py": PysualBuild})
