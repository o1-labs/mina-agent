"""Vendoring landmarks: the pinned download and what it lays down.

No network: the archive is built in-process and handed to fetch() through
the same seam the real download uses.
"""
import hashlib
import io
import tarfile

import pytest

from mina_agent import landmarks as L


def _archive(root=f"landmarks-{L.VERSION}", items=("landmarks.opam", "landmarks-ppx.opam"),
             dirs=("src", "ppx", "src/threads"), lang="(lang dune 3.16)"):
    """A tarball shaped like the upstream one."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add(name, body):
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        add("dune-project", f"{lang}\n(name landmarks)\n".encode())
        for i in items:
            add(i, b"opam-file\n")
        for d in dirs:
            add(f"{d}/dune", b"(library (name x))\n")
    return buf.getvalue()


class _Env:
    repo = "/r"


@pytest.fixture
def vendored(tmp_path, monkeypatch):
    monkeypatch.setattr(L.paths, "state_dir", lambda: tmp_path)
    return tmp_path / "landmarks"


def test_download_refuses_the_wrong_bytes(monkeypatch):
    monkeypatch.setattr(L.urllib.request, "urlopen",
                        lambda url, timeout=None: io.BytesIO(b"not the archive"))
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        L._download("https://example/x.tar.gz", "0" * 64)


def test_download_returns_the_pinned_bytes(monkeypatch):
    data = b"the archive"
    monkeypatch.setattr(L.urllib.request, "urlopen", lambda url, timeout=None: io.BytesIO(data))
    assert L._download("https://example/x.tar.gz", hashlib.sha256(data).hexdigest()) == data


def test_a_download_failure_names_the_url(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("no route to host")
    monkeypatch.setattr(L.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="cannot download landmarks sources from https://example"):
        L._download("https://example/x.tar.gz", "0" * 64)


def test_fetch_lays_out_the_vendored_tree(monkeypatch, vendored):
    monkeypatch.setattr(L, "_download", lambda url, want: _archive())
    dst, msg = L.fetch(_Env())
    assert dst == vendored and "fetched" in msg
    # the dune-project lang line is lowered to what the repo's dune accepts
    assert vendored.joinpath("dune-project").read_text().startswith("(lang dune 3.3)\n")
    # both packages' sources, both .opam files, and nothing else
    assert sorted(p.name for p in vendored.iterdir()) == \
        ["dune-project", "landmarks-ppx.opam", "landmarks.opam", "ppx", "src"]
    # threads needs threads.posix and is unused here
    assert not (vendored / "src" / "threads").exists()
    # the marker that keeps the tree out of every alias and default build
    assert (vendored.parent / "dune").read_text() == L.STATE_DUNE
    assert L.present("/r") and L.status("/r")[0]


def test_fetch_is_idempotent(monkeypatch, vendored):
    monkeypatch.setattr(L, "_download", lambda url, want: _archive())
    L.fetch(_Env())
    monkeypatch.setattr(L, "_download", lambda url, want: pytest.fail("already vendored; must not download"))
    assert L.fetch(_Env())[1] == "present"


def test_fetch_rejects_an_unexpected_layout(monkeypatch, vendored):
    monkeypatch.setattr(L, "_download", lambda url, want: _archive(dirs=("src",)))
    with pytest.raises(RuntimeError, match="not the expected layout: no ppx"):
        L.fetch(_Env())


def test_every_shipped_version_is_pinned():
    assert L.VERSION in L.SHA256 and len(L.SHA256[L.VERSION]) == 64
