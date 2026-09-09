from datetime import datetime, timedelta, timezone

import pytest

from phototripper.track import (
    AFTER,
    BEFORE,
    INSIDE,
    GpsTrack,
    resolve_track_files,
)

GPX_11 = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">
<trk><trkseg>
<trkpt lat="45.0" lon="9.0"><time>2024-06-01T10:00:00Z</time></trkpt>
<trkpt lat="45.1" lon="9.1"><time>2024-06-01T10:01:00Z</time></trkpt>
<trkpt lat="45.5" lon="9.5"><time>2024-06-01T11:30:00Z</time></trkpt>
</trkseg></trk></gpx>
"""

GPX_10 = """<?xml version="1.0"?>
<gpx version="1.0" xmlns="http://www.topografix.com/GPX/1/0">
<wpt lat="10.0" lon="20.0"><time>2024-06-01T09:00:00Z</time></wpt>
<rte><rtept lat="11.0" lon="21.0"><time>2024-06-01T09:30:00+02:00</time></rtept></rte>
<trk><trkseg>
<trkpt lat="12.0" lon="22.0"><time>2024-06-01T12:00:00.500Z</time></trkpt>
<trkpt lat="13.0" lon="23.0"></trkpt>
</trkseg></trk></gpx>
"""


def utc(hour, minute=0, second=0):
    return datetime(2024, 6, 1, hour, minute, second, tzinfo=timezone.utc)


@pytest.fixture
def track(tmp_path):
    (tmp_path / "track.gpx").write_text(GPX_11)
    return GpsTrack.read([str(tmp_path / "track.gpx")])


def test_reads_a_gpx_1_1_track(track):
    assert len(track) == 3
    assert track.start == utc(10)
    assert track.end == utc(11, 30)


def test_reads_waypoints_routes_and_offset_times(tmp_path):
    (tmp_path / "old.gpx").write_text(GPX_10)
    old = GpsTrack.read([str(tmp_path / "old.gpx")])

    # wpt, rtept and trkpt all count; the untimed trkpt does not
    assert len(old) == 3
    assert old.undated == 1

    # 09:30+02:00 is 07:30 UTC, which makes it the earliest point
    assert old.start == utc(7, 30)
    # fractional seconds survive
    assert old.end == utc(12, 0, 0).replace(microsecond=500000)


def test_several_files_merge_in_time_order(tmp_path):
    (tmp_path / "a.gpx").write_text(GPX_11)
    (tmp_path / "b.gpx").write_text(GPX_10)
    merged = GpsTrack.read([str(tmp_path / "a.gpx"), str(tmp_path / "b.gpx")])

    assert len(merged) == 6
    assert [p.time for p in merged.points] == sorted(p.time for p in merged.points)
    assert merged.start == utc(7, 30)


def test_a_broken_file_is_named(tmp_path):
    (tmp_path / "bad.gpx").write_text("<gpx><trk>")
    with pytest.raises(ValueError, match="not a readable GPX file"):
        GpsTrack.read([str(tmp_path / "bad.gpx")])


def test_moment_inside_a_dense_stretch(track):
    coverage = track.locate(utc(10, 0, 30))
    assert coverage.position == INSIDE
    assert coverage.required_secs == 60  # the 10:00 -> 10:01 span


def test_moment_inside_a_gap(track):
    coverage = track.locate(utc(10, 45))
    assert coverage.position == INSIDE
    assert coverage.required_secs == 5340  # the 10:01 -> 11:30 span, 89 minutes


def test_exact_hit_on_a_point(track):
    coverage = track.locate(utc(10, 1))
    assert coverage.position == INSIDE
    assert coverage.required_secs == 0
    assert coverage.latlon == (45.1, 9.1)


def test_moment_before_and_after_the_track(track):
    before = track.locate(utc(9, 30))
    assert before.position == BEFORE
    assert before.required_secs == 1800
    assert before.latlon == (45.0, 9.0)

    after = track.locate(utc(12, 30))
    assert after.position == AFTER
    assert after.required_secs == 3600
    assert after.latlon == (45.5, 9.5)


def test_nearest_point_is_reported(track):
    assert track.locate(utc(10, 0, 10)).latlon == (45.0, 9.0)
    assert track.locate(utc(10, 0, 50)).latlon == (45.1, 9.1)


def test_a_naive_timestamp_is_read_as_utc(tmp_path):
    (tmp_path / "naive.gpx").write_text(GPX_11.replace("T10:00:00Z", "T10:00:00"))
    naive = GpsTrack.read([str(tmp_path / "naive.gpx")])
    assert naive.start == utc(10)


def test_resolve_track_files(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("b.gpx", "a.gpx", "notes.txt"):
        (tmp_path / name).write_text(GPX_11)
    (tmp_path / "sub" / "deep.gpx").write_text(GPX_11)

    # a folder contributes its own tracks, in order, and never recurses
    assert [f.rsplit('/', 1)[-1] for f in resolve_track_files(str(tmp_path))] == [
        "a.gpx", "b.gpx"]

    # a single file, and a comma separated list
    assert resolve_track_files(str(tmp_path / "b.gpx")) == [str(tmp_path / "b.gpx")]
    assert resolve_track_files(f"{tmp_path / 'b.gpx'}, {tmp_path / 'a.gpx'}") == [
        str(tmp_path / "b.gpx"), str(tmp_path / "a.gpx")]


def test_resolve_track_files_reports_what_is_missing(tmp_path):
    with pytest.raises(ValueError, match="Track file not found"):
        resolve_track_files(str(tmp_path / "nope.gpx"))

    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="No gpx track found"):
        resolve_track_files(str(tmp_path / "empty"))


def test_locate_on_an_empty_track():
    assert GpsTrack([], []).locate(utc(10)) is None
    assert GpsTrack([], []).start is None


def test_timezone_aware_comparison_uses_absolute_time(track):
    # 12:45 in CEST is 10:45 UTC, which lands in the gap
    local = datetime(2024, 6, 1, 12, 45, tzinfo=timezone(timedelta(hours=2)))
    assert track.locate(local).required_secs == 5340
