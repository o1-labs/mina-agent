"""The exact dhall CI uses, fetched into harness/state/bin.

CI pins DHALL_VERSION in dockerfiles/Dockerfile-toolchain-base and runs
`dhall --ascii format --check` (buildkite/Makefile check_format). Formatting
differs across dhall releases, so only that exact version is worth running
locally. Upstream shipped 1.30.0 for x86_64 only (linux, macos); on Apple
Silicon it needs Rosetta. This module reads the pin, fetches the matching
release tarball, verifies its sha256, and reports precisely what is missing
when it cannot.
"""
import base64
import hashlib
import io
import os
import platform
import re
import shutil
import sys
import subprocess
import tarfile
import urllib.request

from . import paths

RELEASES = "https://github.com/dhall-lang/dhall-haskell/releases/download/{v}/dhall-{v}-{plat}.tar.bz2"
# sha256 of the release tarballs, from flake.nix (SRI form there)
SHA256 = {
    ("1.30.0", "x86_64-linux"): "sha256-aEVCHenDzED0FAoSeMQI3470m/gIbufzdzDS7ajOlAI=",
    ("1.30.0", "x86_64-macos"): "sha256-mYXQ6yTBv23Fzl2CEAuSvndD3jkNR+XGlHbLEY90RA8=",
}


def pinned_version(repo):
    """DHALL_VERSION from dockerfiles/Dockerfile-toolchain-base."""
    p = os.path.join(repo, "dockerfiles", "Dockerfile-toolchain-base")
    try:
        with open(p) as fh:
            m = re.search(r"^ENV DHALL_VERSION=([0-9.]+)", fh.read(), re.M)
            return m.group(1) if m else None
    except OSError:
        return None


def binary(repo):
    return paths.state_dir() / "bin" / "dhall"


def version_of(path):
    try:
        r = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def rosetta_available():
    return subprocess.run(["arch", "-x86_64", "/usr/bin/true"], capture_output=True).returncode == 0


def platform_for_release():
    """Which release asset this machine can execute, or (None, reason)."""
    sysname, arch = platform.system(), platform.machine()
    if sysname == "Linux" and arch in ("x86_64", "AMD64"):
        return "x86_64-linux", None
    if sysname == "Darwin":
        if arch == "x86_64" or rosetta_available():
            return "x86_64-macos", None
        return None, ("Apple Silicon without Rosetta; the pinned dhall is x86_64-only. "
                      "Install Rosetta: softwareupdate --install-rosetta --agree-to-license")
    return None, f"no pinned dhall build for {sysname}/{arch}"


def _sri_to_hex(sri):
    return base64.b64decode(sri.split("-", 1)[1]).hex()


def fetch(repo):
    """Download and install the pinned dhall. Returns (path, message) or (None, reason)."""
    v = pinned_version(repo)
    if not v:
        return None, "DHALL_VERSION not found in dockerfiles/Dockerfile-toolchain-base"
    dest = binary(repo)
    if dest.exists() and version_of(dest) == v:
        return dest, f"already present ({v})"
    plat, why = platform_for_release()
    if not plat:
        return None, why
    url = RELEASES.format(v=v, plat=plat)
    want = SHA256.get((v, plat))
    data = urllib.request.urlopen(url, timeout=120).read()
    if want:
        got = hashlib.sha256(data).hexdigest()
        if got != _sri_to_hex(want):
            return None, f"sha256 mismatch for {url}: {got}"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tf:
        member = next((m for m in tf.getmembers() if m.name.endswith("/bin/dhall") or m.name == "bin/dhall"), None)
        if not member:
            return None, f"no bin/dhall in {url}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = tf.extractfile(member)
        if src is None:
            return None, f"{member.name} in {url} is not a regular file"
        with src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    dest.chmod(0o755)
    have = version_of(dest)
    if have != v:
        return None, f"downloaded dhall reports {have!r}, expected {v}"
    return dest, f"installed {v} ({plat}{', via Rosetta' if plat == 'x86_64-macos' and platform.machine() == 'arm64' else ''})"


def status(repo):
    """(ok|None, detail) for doctor: exact pinned version in state/bin, or why not."""
    v = pinned_version(repo)
    dest = binary(repo)
    have = version_of(dest) if dest.exists() else None
    if have == v:
        # buildkite/Makefile hardcodes gsed on Darwin; without it every check_* target fails
        if sys.platform == "darwin" and not shutil.which("gsed"):
            return None, f"{dest} ({v}) present but gsed missing (brew install gnu-sed); Lint/Dhall skipped locally"
        return True, f"{dest} ({v}, matches CI)"
    on_path = shutil.which("dhall")
    pv = version_of(on_path) if on_path else None
    plat, why = platform_for_release()
    hint = "run mina-agent setup" if plat else why
    if pv:
        return None, f"PATH has dhall {pv} but CI pins {v}; formatting differs, not used. {hint}"
    return None, f"pinned dhall {v} not installed; Lint/Dhall skipped locally. {hint}"
