import os

import pytest

from phototripper.common import discover_files, expand_path, resolve_target


@pytest.fixture
def shoot(tmp_path):
    """A directory of RAW files with a subfolder and some noise."""
    (tmp_path / "sub").mkdir()
    for name in ("IMG_002.ARW", "IMG_001.arw", "IMG_003.jpg", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "sub" / "IMG_004.arw").write_bytes(b"")
    return tmp_path


def names(paths):
    return [os.path.basename(p) for p in paths]


def test_extension_match_is_case_insensitive(shoot):
    assert names(discover_files(str(shoot))) == ["IMG_001.arw", "IMG_002.ARW"]


def test_results_are_sorted(shoot):
    for name in ("IMG_000.arw", "AAA.arw"):
        (shoot / name).write_bytes(b"")
    assert names(discover_files(str(shoot))) == [
        "AAA.arw", "IMG_000.arw", "IMG_001.arw", "IMG_002.ARW",
    ]


def test_subfolders_need_recursive(shoot):
    assert "IMG_004.arw" not in names(discover_files(str(shoot)))
    assert "IMG_004.arw" in names(discover_files(str(shoot), recursive=True))


def test_name_filter(shoot):
    assert names(discover_files(str(shoot), name_filter="_001")) == ["IMG_001.arw"]
    assert discover_files(str(shoot), name_filter="nothing-matches") == []


def test_custom_extensions(shoot):
    assert names(discover_files(str(shoot), extensions=["jpg"])) == ["IMG_003.jpg"]


def test_extensions_are_not_limited_to_three_characters(shoot):
    (shoot / "IMG_005.tiff").write_bytes(b"")
    assert names(discover_files(str(shoot), extensions=["tiff"])) == ["IMG_005.tiff"]


def test_single_file_target(shoot):
    target = str(shoot / "IMG_001.arw")
    assert discover_files(target) == [target]


def test_single_file_target_still_honours_the_filters(shoot):
    assert discover_files(str(shoot / "notes.txt")) == []
    assert discover_files(str(shoot / "IMG_001.arw"), name_filter="_999") == []


def test_resolve_target_reports_the_containing_folder(shoot):
    root, single = resolve_target(str(shoot / "IMG_001.arw"))
    assert root == str(shoot)
    assert single == str(shoot / "IMG_001.arw")

    root, single = resolve_target(str(shoot))
    assert root == str(shoot)
    assert single is None


def test_resolve_target_rejects_a_missing_path(tmp_path):
    with pytest.raises(ValueError, match="doesn't exist"):
        resolve_target(str(tmp_path / "nope"))


def test_relative_paths_are_expanded(shoot, monkeypatch):
    monkeypatch.chdir(shoot)
    assert names(discover_files(".")) == ["IMG_001.arw", "IMG_002.ARW"]
    assert names(discover_files("sub", recursive=True)) == ["IMG_004.arw"]


def test_expand_path_defaults_to_cwd(shoot, monkeypatch):
    monkeypatch.chdir(shoot)
    assert expand_path(None) == str(shoot)
    assert expand_path("~") == os.path.expanduser("~")
