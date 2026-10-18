"""0.4.1 security fix: downloads never escape their directory and never
clobber silently (REPORT-MCP 2026-10-19, SDK gap 1).

0.4.0 joined the server's Content-Disposition filename onto the output
directory unchecked (``out / server_name``) and overwrote whatever was
there. The filename is chosen by whoever UPLOADED the attachment, so
``../../x`` was a write anywhere the process could reach.
"""
from __future__ import annotations

import os
import urllib.parse

import pytest

import meshbook
from meshbook import MeshbookClient, MeshbookError, safe_filename
from meshbook.client import _filename_from_disposition
from tests._fakes import MESH, Resp, install

HOSTILE = [
    "../../evil.txt",
    "..\\..\\evil.txt",
    "C:\\evil.txt",
    "/etc/passwd",
    "CON",
    "evil\x00.txt",
    "....//....//evil.txt",
    "sub/../../evil.txt",
    "..",
    ".",
    "",
    "nul.tar.gz",
    "C:evil.txt",
    "\\\\server\\share\\evil.txt",
    "evil\u202etxt.exe",
]

ROUTES = {
    "files": "/api/entity-attachments/a1/download",
    "chat": "/api/chat-attachments/a1/download",
    "exports": "/api/mesh-exports/a1/download",
}


@pytest.fixture
def client(tmp_path):
    return MeshbookClient(token="t", mesh=MESH, config_path=tmp_path / "cfg" / "none")


def _disp(name: str) -> dict[str, str]:
    # The RFC 5987 form carries ANY byte, NUL and separators included.
    return {"Content-Disposition": "attachment; filename*=UTF-8''" + urllib.parse.quote(name)}


def _serve(monkeypatch, ns: str, name: str, body: bytes = b"PAYLOAD"):
    r = install(monkeypatch)
    r.add("GET", ROUTES[ns], Resp(body, _disp(name)))
    return r


def _all_files(root) -> set[str]:
    return {
        os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs
    }


# ─── safe_filename: pure, identical on every OS ────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../evil.txt", "evil.txt"),
        ("..\\..\\evil.txt", "evil.txt"),
        ("C:\\evil.txt", "evil.txt"),
        ("/etc/passwd", "passwd"),
        ("CON", "_CON"),
        ("con.txt", "_con.txt"),
        ("nul.tar.gz", "_nul.tar.gz"),
        ("COM1", "_COM1"),
        ("lpt9.log", "_lpt9.log"),
        ("CONSOLE.txt", "CONSOLE.txt"),  # not a device: only the exact stem is
        ("evil\x00.txt", "evil_.txt"),
        ("tab\there", "tab_here"),
        ("C:evil.txt", "C_evil.txt"),
        ("a:b|c?d*e<f>g\"h", "a_b_c_d_e_f_g_h"),
        ("..", "attachment"),
        (".", "attachment"),
        ("", "attachment"),
        (None, "attachment"),
        ("   ", "attachment"),
        (".hidden", "hidden"),
        ("trailing. . ", "trailing"),
        ("evil\u202etxt.exe", "evil_txt.exe"),
        ("report 2026.pdf", "report 2026.pdf"),
        ("żółć.txt", "żółć.txt"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_fallback_is_sanitised_too():
    assert safe_filename("..", fallback="../x.bin") == "x.bin"
    assert safe_filename("", fallback="") == "attachment"


def test_safe_filename_caps_length_and_keeps_the_extension():
    out = safe_filename("a" * 500 + ".pdf")
    assert len(out) <= meshbook.client.MAX_FILENAME and out.endswith(".pdf")
    assert len(safe_filename("b" * 500)) == meshbook.client.MAX_FILENAME


@pytest.mark.parametrize("raw", HOSTILE)
def test_safe_filename_is_one_component_and_never_dots(raw):
    out = safe_filename(raw)
    assert out and out not in (".", "..")
    assert "/" not in out and "\\" not in out and ":" not in out and "\x00" not in out
    assert os.path.basename(out) == out


# ─── the write: contained, never clobbering, atomic ───────────────────


@pytest.mark.parametrize("ns", sorted(ROUTES))
@pytest.mark.parametrize("hostile", HOSTILE)
def test_hostile_server_filename_stays_inside_the_directory(client, monkeypatch, tmp_path, ns,
                                                            hostile):
    root = tmp_path / "jail"
    out_dir = root / "downloads"
    out_dir.mkdir(parents=True)
    _serve(monkeypatch, ns, hostile)
    out = getattr(client, ns).download("a1", out_dir)
    assert out.resolve().parent == out_dir.resolve()
    assert out.read_bytes() == b"PAYLOAD"
    # Nothing anywhere else under the jail, and no temp file left behind.
    assert _all_files(root) == {os.path.join("downloads", out.name)}
    assert not (tmp_path / "evil.txt").exists()


def test_hostile_filename_with_no_out_path_lands_in_cwd(client, monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    _serve(monkeypatch, "files", "../../evil.txt")
    out = client.files.download("a1")
    assert out.resolve() == (work / "evil.txt").resolve()
    assert not (tmp_path / "evil.txt").exists()


def test_existing_file_is_refused_without_overwrite(client, monkeypatch, tmp_path):
    (tmp_path / "spec.pdf").write_bytes(b"MINE")
    _serve(monkeypatch, "files", "spec.pdf", b"THEIRS")
    with pytest.raises(MeshbookError) as ei:
        client.files.download("a1", tmp_path)
    assert ei.value.code == "file_exists"
    assert (tmp_path / "spec.pdf").read_bytes() == b"MINE"
    assert _all_files(tmp_path) == {"spec.pdf"}  # no temp file left


def test_overwrite_true_replaces(client, monkeypatch, tmp_path):
    (tmp_path / "spec.pdf").write_bytes(b"MINE")
    _serve(monkeypatch, "chat", "spec.pdf", b"THEIRS")
    out = client.chat.download("a1", tmp_path, overwrite=True)
    assert out.read_bytes() == b"THEIRS"
    assert _all_files(tmp_path) == {"spec.pdf"}


def test_explicit_existing_file_is_refused_before_any_request(client, monkeypatch, tmp_path):
    target = tmp_path / "backup.zip"
    target.write_bytes(b"OLD")
    r = _serve(monkeypatch, "exports", "x.zip", b"NEW")
    with pytest.raises(MeshbookError) as ei:
        client.exports.download("a1", target)
    assert ei.value.code == "file_exists"
    assert r.requests == []  # a refusal costs no download
    assert target.read_bytes() == b"OLD"
    assert client.exports.download("a1", target, overwrite=True).read_bytes() == b"NEW"


def test_explicit_file_path_ignores_the_server_name(client, monkeypatch, tmp_path):
    _serve(monkeypatch, "files", "../../evil.txt")
    out = client.files.download("a1", tmp_path / "mine.bin")
    assert out == tmp_path / "mine.bin" and out.read_bytes() == b"PAYLOAD"


def test_trailing_separator_means_a_directory(client, monkeypatch, tmp_path):
    _serve(monkeypatch, "files", "spec.pdf")
    out = client.files.download("a1", str(tmp_path / "newdir") + os.sep)
    assert out == tmp_path / "newdir" / "spec.pdf"


def test_symlink_planted_under_the_safe_name_is_refused(client, monkeypatch, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"PRECIOUS")
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()
    try:
        os.symlink(outside, out_dir / "evil.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    _serve(monkeypatch, "files", "../evil.txt")
    for overwrite in (False, True):
        with pytest.raises(MeshbookError) as ei:
            client.files.download("a1", out_dir, overwrite=overwrite)
        assert ei.value.code == "unsafe_path"
    assert outside.read_bytes() == b"PRECIOUS"


def test_no_hard_links_falls_back_but_still_refuses_existing(client, monkeypatch, tmp_path):
    def no_link(src, dst):
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", no_link)
    _serve(monkeypatch, "files", "spec.pdf", b"ONE")
    out = client.files.download("a1", tmp_path)
    assert out.read_bytes() == b"ONE"
    with pytest.raises(MeshbookError) as ei:
        client.files.download("a1", tmp_path)
    assert ei.value.code == "file_exists"
    assert _all_files(tmp_path) == {"spec.pdf"}


def test_a_failed_write_is_typed_and_leaves_no_temp_file(client, monkeypatch, tmp_path):
    def boom(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", boom)
    _serve(monkeypatch, "files", "spec.pdf")
    with pytest.raises(MeshbookError) as ei:
        client.files.download("a1", tmp_path, overwrite=True)
    assert ei.value.code == "write_failed"
    assert _all_files(tmp_path) == set()


# ─── download_bytes / download_raw ─────────────────────────────────────


@pytest.mark.parametrize("ns", sorted(ROUTES))
def test_download_bytes_returns_a_sanitised_name_and_writes_nothing(client, monkeypatch,
                                                                     tmp_path, ns):
    monkeypatch.chdir(tmp_path)
    _serve(monkeypatch, ns, "..\\..\\evil.txt", b"BYTES")
    got = getattr(client, ns).download_bytes("a1")
    filename, content = got
    assert (filename, content) == ("evil.txt", b"BYTES")
    assert got.filename == "evil.txt" and got.content == b"BYTES"
    assert _all_files(tmp_path) == set()


def test_download_bytes_takes_a_per_call_mesh(client, monkeypatch):
    r = _serve(monkeypatch, "files", "a.txt")
    other = "33333333-3333-3333-3333-333333333333"
    client.files.download_bytes("a1", mesh=other)
    assert r.last.get_header("X-active-mesh-id") == other


@pytest.mark.raw_request
def test_download_raw_takes_a_per_call_mesh_and_stays_unsanitised(client, monkeypatch):
    r = _serve(monkeypatch, "files", "../x.txt")
    other = "33333333-3333-3333-3333-333333333333"
    content, name = client.download_raw(ROUTES["files"], mesh=other)
    assert r.last.get_header("X-active-mesh-id") == other
    assert (content, name) == (b"PAYLOAD", "../x.txt")


# ─── Content-Disposition parsing (squashed alongside) ──────────────────


@pytest.mark.parametrize(
    "header, expected",
    [
        ('attachment; filename="spec.pdf"', "spec.pdf"),
        ("attachment; filename=spec.pdf", "spec.pdf"),  # bare token: was "attachment"
        ("attachment; FILENAME=\"Spec.pdf\"", "Spec.pdf"),
        ("attachment; filename*=utf-8''%C5%BC%C3%B3%C5%82w.txt", "żółw.txt"),  # lower-case
        ("attachment; filename*=UTF-8'en'na%20me.txt", "na me.txt"),  # language tag
        ("attachment; filename=\"a.txt\"; filename*=UTF-8''b.txt", "b.txt"),
        ('attachment; filename="say \\"hi\\".txt"', 'say "hi".txt'),
        ("attachment", "attachment"),
        ("", "attachment"),
    ],
)
def test_filename_from_disposition(header, expected):
    assert _filename_from_disposition(header) == expected
