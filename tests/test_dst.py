"""The camera time check: does OffsetTimeOriginal match where the track says we were?"""
import logging
import shutil
import subprocess

import pytest

from phototripper import gpx
from phototripper.common import Context
from phototripper.gpx import resolve_timezones

from .conftest import MINIMAL_GPX, write_arw
from .conftest import GpxArgs as Args

MILAN = (45.4642, 9.19)


@pytest.fixture(autouse=True)
def needs_geolocation_db():
    if not shutil.which("exiftool"):
        pytest.skip("exiftool is not installed")
    if not resolve_timezones([MILAN]):
        pytest.skip("this exiftool build has no geolocation database")


def test_resolve_timezones_is_offline_and_batched(monkeypatch):
    calls = []
    real = subprocess.run
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: (calls.append(a), real(*a, **kw))[1])

    zones = resolve_timezones([MILAN, (48.8566, 2.3522), (35.6762, 139.6503)])

    assert len(calls) == 1, "every location must share one exiftool call"
    assert zones[MILAN] == "Europe/Rome"
    assert zones[(48.8566, 2.3522)] == "Europe/Paris"
    assert zones[(35.6762, 139.6503)] == "Asia/Tokyo"


@pytest.fixture
def camera(tmp_path, exiftool, caplog):
    """A shoot in Milan whose camera offset is whatever the test asks for."""

    def _run(offset, date="2024:06:01", track_date="2024-06-01", **kwargs):
        (tmp_path / "track.gpx").write_text(
            MINIMAL_GPX.replace("2024-06-01", track_date))

        picture = tmp_path / "IMG_001.arw"
        write_arw(picture)
        exiftool(f"-DateTimeOriginal={date} 12:00:30",
                 f"-OffsetTimeOriginal={offset}", str(picture))

        kwargs.setdefault('skip_dst_check', False)

        caplog.clear()
        Context.context = None
        with caplog.at_level(logging.DEBUG):
            gpx.run(Args(search_dir=str(tmp_path),
                         track=str(tmp_path / "track.gpx"),
                         dry_run=True, **kwargs))
        return "\n".join(r.getMessage() for r in caplog.records)

    return _run


def test_a_correct_camera_says_nothing(camera):
    # June in Milan is CEST, so +02:00 is right
    out = camera("+02:00")
    assert "Camera time" not in out
    assert "the camera's UTC offset matches every location" in out


def test_daylight_saving_left_switched_off(camera):
    out = camera("+01:00")

    assert "| Europe/Rome | +01:00   | +02:00" in out
    assert "left switched off" in out
    assert ("exiftool -P -overwrite_original "
            "-AllDates+=1 -OffsetTimeOriginal=+02:00") in out


def test_daylight_saving_left_switched_on(camera):
    # January in Milan is CET, so a camera still on +02:00 is an hour ahead
    out = camera("+02:00", date="2024:01:15", track_date="2024-01-15")

    assert "| Europe/Rome | +02:00   | +01:00" in out
    assert "left switched on" in out
    assert ("exiftool -P -overwrite_original "
            "-AllDates-=1 -OffsetTimeOriginal=+01:00") in out


def test_a_gross_mismatch_is_not_blamed_on_daylight_saving(camera):
    out = camera("+05:00")

    assert "| Europe/Rome | +05:00   | +02:00" in out
    assert "daylight saving" not in out
    assert "geotagged 3h off" in out


def test_both_readings_are_offered(camera):
    """EXIF cannot tell a shifted clock from a wrong offset, so say both."""
    out = camera("+01:00")

    assert "If the clock was set to match the offset" in out
    assert "If instead the clock was right and only the offset wrong" in out
    assert "geotagged 1h off" in out


def test_the_check_can_be_turned_off(camera):
    assert "Camera time" not in camera("+01:00", skip_dst_check=True)


def test_the_check_never_changes_the_geotagging(tmp_path, exiftool):
    """A camera left on winter time still lands in the right place."""
    (tmp_path / "track.gpx").write_text(MINIMAL_GPX)
    picture = tmp_path / "IMG_001.arw"
    write_arw(picture)
    # 12:00:30 at +01:00 is 11:00:30 UTC, inside the track's 10:00-11:30 span
    exiftool("-DateTimeOriginal=2024:06:01 12:00:30",
             "-OffsetTimeOriginal=+01:00", str(picture))

    Context.context = None
    gpx.run(Args(search_dir=str(tmp_path), track=str(tmp_path / "track.gpx"),
                 max_int_secs=5340, dry_run=False))

    out = subprocess.run(["exiftool", "-n", "-s3", "-GPSLatitude", str(picture)],
                         capture_output=True, text=True).stdout.strip()
    assert out, "the picture is geotagged despite the camera time being off"
    assert 45.46 < float(out) < 45.51
