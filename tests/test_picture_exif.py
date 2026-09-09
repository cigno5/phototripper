from datetime import datetime, timedelta, timezone

import pytest

from phototripper.common import (
    Context,
    FileSettings,
    LocationSettings,
    LoggingSettings,
)
from phototripper.picture import PictureInfo, format_utc_offset, parse_utc_offset


@pytest.fixture
def context():
    ctx = Context(
        LocationSettings('none', None, None),
        LoggingSettings(False, False, False),
        FileSettings(None, False, None, True, False, None),
    )
    Context.set(ctx)
    return ctx


def picture(context, **tags):
    return PictureInfo("/nowhere/IMG_0001.arw", tags=tags)


def test_parse_utc_offset():
    assert parse_utc_offset("+02:00") == timezone(timedelta(hours=2))
    assert parse_utc_offset("-05:30") == timezone(timedelta(hours=-5, minutes=-30))
    assert parse_utc_offset("+00:00") == timezone(timedelta(0))


@pytest.mark.parametrize("value", ["", "02:00", "+2:00", "+02:00:00", "abc", "Z"])
def test_parse_utc_offset_rejects_junk(value):
    with pytest.raises(ValueError):
        parse_utc_offset(value)


@pytest.mark.parametrize("value", ["+02:00", "-05:30", "+00:00", "+14:00"])
def test_format_utc_offset_round_trips(value):
    assert format_utc_offset(parse_utc_offset(value)) == value


def test_utc_datetime_subtracts_the_camera_offset(context):
    # the spec's example: shooting at 12:35 local in CEST is 10:35 UTC
    pic = picture(
        context,
        DateTimeOriginal=datetime(2024, 6, 1, 12, 35, 0),
        OffsetTimeOriginal=parse_utc_offset("+02:00"),
    )
    assert pic.get_utc_datetime() == datetime(2024, 6, 1, 10, 35, tzinfo=timezone.utc)


def test_utc_datetime_handles_a_negative_offset(context):
    pic = picture(
        context,
        DateTimeOriginal=datetime(2024, 6, 1, 8, 0, 0),
        OffsetTimeOriginal=parse_utc_offset("-05:00"),
    )
    assert pic.get_utc_datetime() == datetime(2024, 6, 1, 13, 0, tzinfo=timezone.utc)


def test_utc_datetime_needs_both_tags(context):
    no_offset = picture(context, DateTimeOriginal=datetime(2024, 6, 1, 12, 0))
    assert no_offset.get_utc_datetime() is None
    assert no_offset.get_utc_offset() is None
    assert no_offset.has_date_time()

    no_datetime = picture(context, OffsetTimeOriginal=parse_utc_offset("+02:00"))
    assert no_datetime.get_utc_datetime() is None
    assert not no_datetime.has_date_time()

    assert picture(context).get_utc_datetime() is None


def test_unreadable_values_are_dropped_not_fatal():
    raw = {
        "DateTimeOriginal": "2024:06:01 12:00:30",
        "OffsetTimeOriginal": "not an offset",
        "SequenceNumber": "not a number",
        "GPSLatitude": 45.4671,
        "UnknownTag": "whatever",
    }
    tags = PictureInfo._convert_tags(raw, "IMG_0001.arw")

    assert tags["DateTimeOriginal"] == datetime(2024, 6, 1, 12, 0, 30)
    assert tags["GPSLatitude"] == 45.4671
    for dropped in ("OffsetTimeOriginal", "SequenceNumber", "UnknownTag"):
        assert dropped not in tags


@pytest.fixture
def shoot(tmp_path):
    """Two real files exiftool can read, plus one it can make nothing of."""
    pytest.importorskip("shutil")
    import shutil
    import subprocess

    if not shutil.which("exiftool"):
        pytest.skip("exiftool is not installed")

    jpeg = bytes.fromhex(
        'ffd8ffe000104a46494600010100000100010000ffdb004300'
        + 'ff' * 64
        + 'ffc0000b080001000101011100ffc40014000100000000000000000000000000000003'
        'ffda0008010100003f00d2cf20ffd9'
    )
    for name in ("IMG_001.jpg", "IMG_002.jpg"):
        (tmp_path / name).write_bytes(jpeg)
    (tmp_path / "IMG_003.jpg").write_text("not an image at all")

    subprocess.run(["exiftool", "-q", "-overwrite_original",
                    "-DateTimeOriginal=2024:06:01 12:35:00",
                    "-OffsetTimeOriginal=+02:00",
                    str(tmp_path / "IMG_001.jpg")], check=True)
    subprocess.run(["exiftool", "-q", "-overwrite_original",
                    "-DateTimeOriginal=2024:06:01 13:00:00",
                    str(tmp_path / "IMG_002.jpg")], check=True)
    return tmp_path


def test_scan_reads_every_file_in_one_call(context, shoot, monkeypatch):
    import subprocess

    calls = []
    real = subprocess.check_output
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])

    files = [str(shoot / n) for n in ("IMG_001.jpg", "IMG_002.jpg", "IMG_003.jpg")]
    pictures = PictureInfo.scan(files)

    assert len(calls) == 1, "the whole shoot must cost a single exiftool call"
    assert [p.file for p in pictures] == files, "order is preserved"

    assert pictures[0].get_utc_datetime() == datetime(
        2024, 6, 1, 10, 35, tzinfo=timezone.utc)
    assert pictures[1].get_utc_datetime() is None  # has no OffsetTimeOriginal
    assert pictures[1].has_date_time()
    assert not pictures[2].has_date_time()  # unreadable file, no tags at all


def test_scan_of_nothing(context):
    assert PictureInfo.scan([]) == []
