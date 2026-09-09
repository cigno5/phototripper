import os
import shutil
import struct
import subprocess

import pytest

from phototripper import common
from phototripper.common import Context


@pytest.fixture(autouse=True)
def reset_context():
    """``Context.set`` refuses a second call, so clear the singleton per test."""
    Context.context = None
    yield
    Context.context = None


# A real 'gpsbabel -^3' dump, so nothing here has to guess what it prints.
FORMATS_DUMP = os.path.join(os.path.dirname(__file__), "data",
                            "gpsbabel_formats.txt")


@pytest.fixture
def gpsbabel_formats(monkeypatch):
    """Answer "what can this gpsbabel do?" from the dump, not from the binary."""
    from phototripper import gpsbabel

    with open(FORMATS_DUMP) as handle:
        formats = gpsbabel.parse_formats(handle.read())

    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: formats)
    return formats


@pytest.fixture
def ini(tmp_path, monkeypatch):
    """Write a phototripper.ini and make the config lookup find it."""

    def _write(text):
        path = tmp_path / common.APP_CONFIG_FILENAME
        path.write_text(text)
        monkeypatch.chdir(tmp_path)
        return path

    return _write


MINIMAL_GPX = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
<trk><trkseg>
<trkpt lat="45.4642" lon="9.1900"><time>2024-06-01T10:00:00Z</time></trkpt>
<trkpt lat="45.4700" lon="9.2000"><time>2024-06-01T10:01:00Z</time></trkpt>
<trkpt lat="45.5000" lon="9.2500"><time>2024-06-01T11:30:00Z</time></trkpt>
</trkseg></trk></gpx>
"""


def write_arw(path):
    """Write the smallest file exiftool accepts as a writable ARW.

    ARW is a TIFF flavour, so a bare TIFF with the mandatory baseline tags is
    enough to exercise the real exiftool round trip without shipping a RAW.
    """
    entries = [(0x0100, 3, 1, 1), (0x0101, 3, 1, 1), (0x0102, 3, 1, 8),
               (0x0103, 3, 1, 1), (0x0106, 3, 1, 1), (0x0111, 4, 1, 0),
               (0x0116, 4, 1, 1), (0x0117, 4, 1, 1)]
    strip_offset = 8 + 2 + len(entries) * 12 + 4

    ifd = struct.pack('<H', len(entries))
    for tag, kind, count, value in entries:
        if tag == 0x0111:
            value = strip_offset
        ifd += struct.pack('<HHI', tag, kind, count)
        ifd += struct.pack('<HH', value, 0) if kind == 3 else struct.pack('<I', value)
    ifd += struct.pack('<I', 0)

    path.write_bytes(struct.pack('<2sHI', b'II', 42, 8) + ifd + b'\x00')


@pytest.fixture
def exiftool():
    if not shutil.which("exiftool"):
        pytest.skip("exiftool is not installed")

    def _run(*args):
        subprocess.run(["exiftool", "-q", "-overwrite_original", *args], check=True)

    return _run


@pytest.fixture
def shoot(tmp_path, exiftool):
    """A track plus five pictures, one per situation gpx has to report."""
    (tmp_path / "track.gpx").write_text(MINIMAL_GPX)

    for name in ("IMG_001", "IMG_002", "IMG_003", "IMG_004", "IMG_005"):
        write_arw(tmp_path / f"{name}.arw")

    def tag(name, *args):
        exiftool(*args, str(tmp_path / f"{name}.arw"))

    # inside the track
    tag("IMG_001", "-DateTimeOriginal=2024:06:01 12:00:30",
        "-OffsetTimeOriginal=+02:00")
    # inside the 89 minute gap
    tag("IMG_002", "-DateTimeOriginal=2024:06:01 12:45:00",
        "-OffsetTimeOriginal=+02:00")
    # an hour past the end of the track
    tag("IMG_003", "-DateTimeOriginal=2024:06:01 14:30:00",
        "-OffsetTimeOriginal=+02:00")
    # the camera never recorded a UTC offset
    tag("IMG_004", "-DateTimeOriginal=2024:06:01 12:10:00")
    # already carries coordinates
    tag("IMG_005", "-DateTimeOriginal=2024:06:01 12:20:00",
        "-OffsetTimeOriginal=+02:00",
        "-GPSLatitude=45.0", "-GPSLatitudeRef=N",
        "-GPSLongitude=9.0", "-GPSLongitudeRef=E")

    return tmp_path


class GpxArgs:
    """The argparse namespace gpx.run consumes, with the config defaults."""

    def __init__(self, **kwargs):
        self.__dict__.update(
            search_dir=None, track=None, filter=None, recursive=None,
            sidecar=False, overwrite_gps=False, max_int_secs=None,
            max_ext_secs=None, geosync=None, skip_dst_check=True,
            verbose=None, summary=None, dry_run=True,
            extract=None, wipe=None, logger=None, yes=None,
            date_from=None, date_to=None,
        )
        self.__dict__.update(kwargs)


@pytest.fixture
def gpx_args():
    return GpxArgs
