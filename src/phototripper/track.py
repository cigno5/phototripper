import bisect
import glob
import logging
import os
from collections import namedtuple
from datetime import datetime, timezone
from xml.etree import ElementTree

TRACK_EXT = ["gpx"]

# exiftool's own defaults for -api GeoMaxIntSecs / GeoMaxExtSecs
DEFAULT_MAX_INT_SECS = 1800
DEFAULT_MAX_EXT_SECS = 1800

# A point of the recorded track: when the logger was where.
TrackPoint = namedtuple("TrackPoint", "time, lat, lon")

# Where a moment falls relative to the track.
INSIDE = 'inside'
BEFORE = 'before'
AFTER = 'after'

# How the track covers a given moment.
#
#   position      -- INSIDE, BEFORE or AFTER
#   required_secs -- the tolerance exiftool needs to accept it: the span
#                    between the two surrounding points when INSIDE, the
#                    distance to the nearest end otherwise
#   latlon        -- the nearest recorded position, for reporting
Coverage = namedtuple("Coverage", "position, required_secs, latlon")


def resolve_track_files(track):
    """Turn the ``--track`` setting into the list of track files to read.

    *track* is a file, a comma separated list of files, or a folder. A folder
    contributes the track files sitting directly in it: the lookup is never
    recursive, so scanning pictures recursively can't quietly pull in a
    stray track from a subfolder.
    """
    files = []

    for entry in [e.strip() for e in track.split(',') if e.strip()]:
        path = os.path.abspath(os.path.expanduser(entry))

        if os.path.isdir(path):
            found = sorted(f for ext in TRACK_EXT
                           for f in glob.glob(os.path.join(path, f"*.{ext}")))
            if not found:
                raise ValueError(f"No {'/'.join(TRACK_EXT)} track found in {path}")
            files.extend(found)

        elif os.path.isfile(path):
            files.append(path)

        else:
            raise ValueError(f"Track file not found: {path}")

    return files


def _local_name(element):
    """The tag name of *element* without its XML namespace."""
    return element.tag.rsplit('}', 1)[-1]


def _read_point(element):
    """Build a TrackPoint from a trkpt/rtept/wpt element, or None."""
    stamp = next((c for c in element if _local_name(c) == 'time'), None)
    if stamp is None or not stamp.text:
        return None

    when = datetime.fromisoformat(stamp.text.strip())
    if when.tzinfo is None:
        # GPX mandates UTC; a logger omitting the zone still means UTC
        when = when.replace(tzinfo=timezone.utc)

    return TrackPoint(when.astimezone(timezone.utc),
                      float(element.get('lat')),
                      float(element.get('lon')))


class GpsTrack:
    """The merged, time ordered points of one or more GPS track files."""

    POINT_TAGS = {'trkpt', 'rtept', 'wpt'}

    def __init__(self, files, points, undated=0):
        self.files = files
        self.points = points
        self.undated = undated
        self._times = [p.time for p in points]

    @classmethod
    def read(cls, files):
        """Read and merge every file in *files*."""
        points = []
        undated = 0

        for file in files:
            try:
                root = ElementTree.parse(file).getroot()
            except ElementTree.ParseError as error:
                raise ValueError(
                    f"{file} is not a readable GPX file: {error}") from error

            found = 0
            for element in root.iter():
                if _local_name(element) not in cls.POINT_TAGS:
                    continue

                point = _read_point(element)
                if point is None:
                    undated += 1
                else:
                    points.append(point)
                    found += 1

            logging.debug(f" > {os.path.basename(file)}: {found} timed points")

        return cls(files, sorted(points), undated)

    def __len__(self):
        return len(self.points)

    @property
    def start(self):
        return self.points[0].time if self.points else None

    @property
    def end(self):
        return self.points[-1].time if self.points else None

    def locate(self, when):
        """Describe how the track covers the moment *when*."""
        if not self.points:
            return None

        index = bisect.bisect_left(self._times, when)

        if index == 0 and when < self.start:
            return Coverage(BEFORE, (self.start - when).total_seconds(),
                            (self.points[0].lat, self.points[0].lon))

        if index >= len(self.points):
            return Coverage(AFTER, (when - self.end).total_seconds(),
                            (self.points[-1].lat, self.points[-1].lon))

        after = self.points[index]
        if after.time == when:
            return Coverage(INSIDE, 0, (after.lat, after.lon))

        before = self.points[index - 1]
        nearest = before if (when - before.time) <= (after.time - when) else after

        return Coverage(INSIDE, (after.time - before.time).total_seconds(),
                        (nearest.lat, nearest.lon))
