import logging

import pytest

from phototripper import gpx
from phototripper.common import Context
from phototripper.gpx import humanize

from .conftest import GpxArgs as Args


@pytest.fixture
def report(shoot, caplog):
    """Run gpx over the fixture shoot and hand back what it printed."""

    def _run(**kwargs):
        caplog.clear()
        Context.context = None  # one run per process is all the CLI ever needs
        with caplog.at_level(logging.DEBUG):
            gpx.run(Args(search_dir=str(shoot),
                         track=str(shoot / "track.gpx"),
                         **kwargs))
        return "\n".join(r.getMessage() for r in caplog.records)

    return _run


@pytest.mark.parametrize("seconds, expected", [
    (0, "0 s"), (59, "59 s"), (60, "1 min"), (5340, "89 min"),
    (7200, "2.0 h"), (172800, "2.0 days"),
])
def test_humanize(seconds, expected):
    assert humanize(seconds) == expected


def test_every_situation_is_reported_once(report):
    out = report()

    assert "| gap " in out
    assert "| after-track " in out
    assert "| no-offset " in out
    assert "| has-gps " in out
    assert "1 of 5 picture(s) can be geotagged" in out


def test_the_suggested_tolerance_is_the_one_that_works(report):
    # the gap between the two surrounding points is exactly 89 minutes, and
    # exiftool's own comparison is inclusive
    assert "--max-int-secs 5340" in report()

    widened = report(max_int_secs=5340)
    assert "| gap " not in widened
    assert "2 of 5 picture(s) can be geotagged" in widened

    assert "| gap " in report(max_int_secs=5339)


def test_extrapolation_tolerance(report):
    assert "--max-ext-secs 3600" in report()
    assert "| after-track " not in report(max_ext_secs=3600)


def test_existing_coordinates_are_kept_unless_forced(report):
    assert "| has-gps " in report()
    assert "use --overwrite-gps" in report()
    assert "| has-gps " not in report(overwrite_gps=True)


def test_a_clean_run_says_so(report):
    out = report(max_int_secs=5340, max_ext_secs=3600, overwrite_gps=True)
    assert "| no-offset " in out, "the missing offset is never silently fixed"

    # everything except the picture with no offset at all
    assert "4 of 5 picture(s) can be geotagged" in out


def test_verbose_names_the_affected_files(report):
    out = report(verbose=True)
    assert "IMG_002.arw: falls in a gap in the track" in out
    assert "IMG_004.arw: no OffsetTimeOriginal" in out


def test_a_track_from_another_day_is_called_out(shoot, caplog):
    other = shoot / "other.gpx"
    other.write_text(
        (shoot / "track.gpx").read_text().replace("2024-06-01", "2023-01-15"))

    with caplog.at_level(logging.DEBUG):
        gpx.run(Args(search_dir=str(shoot), track=str(other), overwrite_gps=True))
    out = "\n".join(r.getMessage() for r in caplog.records)

    assert "Not a single picture falls near the track" in out
    assert "too far off to be a tolerance problem" in out, \
        "a 500 day tolerance is not a suggestion worth making"
    assert "0 of 5 picture(s) can be geotagged" in out


def test_a_track_is_mandatory(shoot):
    with pytest.raises(ValueError, match="No GPS track specified"):
        gpx.run(Args(search_dir=str(shoot)))


def latlon(path):
    """The coordinates a file carries, or None."""
    import subprocess
    out = subprocess.run(["exiftool", "-n", "-s3", "-GPSLatitude", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out) if out else None


@pytest.fixture
def geotag(shoot):
    """Actually run the geotagging over the fixture shoot."""

    def _run(**kwargs):
        Context.context = None
        kwargs.setdefault('dry_run', False)
        gpx.run(Args(search_dir=str(shoot), track=str(shoot / "track.gpx"),
                     **kwargs))

    return _run


def test_dry_run_writes_nothing(shoot, geotag):
    geotag(dry_run=True, max_int_secs=5340, max_ext_secs=3600, overwrite_gps=True)

    assert latlon(shoot / "IMG_001.arw") is None
    assert latlon(shoot / "IMG_005.arw") == 45.0  # untouched
    assert not list(shoot.glob("*.xmp"))


def test_only_the_placeable_pictures_are_written(shoot, geotag):
    geotag()

    assert latlon(shoot / "IMG_001.arw") == pytest.approx(45.4671, abs=1e-4)
    assert latlon(shoot / "IMG_002.arw") is None  # in the gap
    assert latlon(shoot / "IMG_003.arw") is None  # past the end
    assert latlon(shoot / "IMG_004.arw") is None  # no UTC offset
    assert latlon(shoot / "IMG_005.arw") == 45.0  # kept its own coordinates


def test_existing_coordinates_survive_a_default_run(shoot, geotag):
    geotag(max_int_secs=5340, max_ext_secs=3600)
    assert latlon(shoot / "IMG_005.arw") == 45.0

    geotag(max_int_secs=5340, max_ext_secs=3600, overwrite_gps=True)
    assert latlon(shoot / "IMG_005.arw") != 45.0


def test_widened_tolerances_reach_the_rest(shoot, geotag):
    geotag(max_int_secs=5340, max_ext_secs=3600)

    assert latlon(shoot / "IMG_002.arw") == pytest.approx(45.4848, abs=1e-4)
    assert latlon(shoot / "IMG_003.arw") == pytest.approx(45.5, abs=1e-4)
    assert latlon(shoot / "IMG_004.arw") is None, "a missing offset is never guessed"


def test_sidecar_mode_leaves_the_raw_alone(shoot, geotag):
    geotag(sidecar=True, max_int_secs=5340, max_ext_secs=3600)

    assert latlon(shoot / "IMG_001.xmp") == pytest.approx(45.4671, abs=1e-4)
    assert latlon(shoot / "IMG_001.arw") is None
    assert latlon(shoot / "IMG_005.arw") == 45.0


def test_an_existing_sidecar_is_not_replaced(shoot, geotag, caplog):
    geotag(sidecar=True)
    before = (shoot / "IMG_001.xmp").read_text()

    with caplog.at_level(logging.INFO):
        geotag(sidecar=True)
    assert "has-sidecar" in "\n".join(r.getMessage() for r in caplog.records)
    assert (shoot / "IMG_001.xmp").read_text() == before


def test_forcing_updates_an_existing_sidecar(shoot, geotag):
    geotag(sidecar=True)
    assert latlon(shoot / "IMG_001.xmp") == pytest.approx(45.4671, abs=1e-4)

    geotag(sidecar=True, overwrite_gps=True, max_int_secs=5340)
    # IMG_002 now gets one too, and IMG_001 is rewritten rather than refused
    assert latlon(shoot / "IMG_002.xmp") == pytest.approx(45.4848, abs=1e-4)
    assert latlon(shoot / "IMG_001.xmp") == pytest.approx(45.4671, abs=1e-4)


def test_the_counts_come_from_exiftool(shoot, geotag, caplog):
    with caplog.at_level(logging.INFO):
        geotag(max_int_secs=5340, max_ext_secs=3600, overwrite_gps=True)

    # 4 written, only the picture without a UTC offset held back
    assert "4 geotagged, 1 skipped, 0 failed" in \
        "\n".join(r.getMessage() for r in caplog.records)


def test_settings_come_from_the_config_file(shoot, monkeypatch, caplog):
    (shoot / "phototripper.ini").write_text(f"""
[gpx]
track = {shoot / "track.gpx"}
max-int-secs = 5340
overwrite-gps = true
""")
    monkeypatch.chdir(shoot)

    from phototripper.common import apply_config_to_args

    args = Args(search_dir=str(shoot), track=None, max_int_secs=None,
                overwrite_gps=None, dry_run=False)
    apply_config_to_args(args, "gpx")

    Context.context = None
    with caplog.at_level(logging.INFO):
        gpx.run(args)

    assert latlon(shoot / "IMG_002.arw") == pytest.approx(45.4848, abs=1e-4)
    assert latlon(shoot / "IMG_005.arw") != 45.0
