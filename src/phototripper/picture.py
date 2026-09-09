import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from functools import reduce
from math import sqrt

from .common import Context, haversine

OFFSET_RE = re.compile(r"^(?P<sign>[-+])(?P<hours>\d{2}):(?P<minutes>\d{2})$")


def parse_utc_offset(value):
    """Turn an EXIF offset such as ``+02:00`` into a :class:`timezone`."""
    match = OFFSET_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"Not a valid UTC offset: {value}")

    sign = -1 if match.group('sign') == '-' else 1
    delta = timedelta(hours=int(match.group('hours')),
                      minutes=int(match.group('minutes')))
    return timezone(sign * delta)


def format_utc_offset(tz):
    """Render a :class:`timezone` back as an EXIF offset such as ``+02:00``."""
    total = int(tz.utcoffset(None).total_seconds())
    sign = '-' if total < 0 else '+'
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


class PictureInfo:
    T_DATE_TIME_ORIGINAL = "DateTimeOriginal"
    T_OFFSET_TIME_ORIGINAL = "OffsetTimeOriginal"
    T_SEQUENCE_NUMBER = "SequenceNumber"
    T_GPS_LATITUDE = "GPSLatitude"
    T_GPS_LONGITUDE = "GPSLongitude"

    TAGS = {
        T_DATE_TIME_ORIGINAL: lambda x: datetime.strptime(x, "%Y:%m:%d %H:%M:%S"),
        T_OFFSET_TIME_ORIGINAL: parse_utc_offset,
        T_SEQUENCE_NUMBER: lambda x: int(x),
        T_GPS_LATITUDE: lambda x: float(x),
        T_GPS_LONGITUDE: lambda x: float(x),
    }

    @staticmethod
    def _convert_tags(raw, file):
        """Convert the raw exiftool values, dropping any the camera mangled."""
        tags = {}
        for tag, value in raw.items():
            try:
                tags[tag] = PictureInfo.TAGS[tag](value)
            except (KeyError, TypeError, ValueError):
                logging.debug(f" > ignoring unreadable {tag} '{value}' in {file}")
        return tags

    @staticmethod
    def _read_tags_batch(files):
        """Read the tags of many files with a single exiftool call.

        The file list goes through an argument file, so a large shoot cannot
        overflow the command line.
        """
        with tempfile.NamedTemporaryFile('w', suffix='.args', delete=False) as arg_file:
            arg_file.write("\n".join(files))
            arg_file_name = arg_file.name

        try:
            out = subprocess.check_output(["exiftool", "-n", "-j",
                                           *['-' + t for t in PictureInfo.TAGS.keys()],
                                           "-@", arg_file_name],
                                          stderr=subprocess.DEVNULL)
        finally:
            os.unlink(arg_file_name)

        by_file = {}
        for entry in json.loads(out.decode('utf-8') or '[]'):
            source = os.path.normpath(entry.pop("SourceFile"))
            by_file[source] = PictureInfo._convert_tags(entry, source)

        return by_file

    @classmethod
    def scan(cls, files):
        """Build the ``PictureInfo`` of every file, reading them all at once."""
        if not files:
            return []

        by_file = cls._read_tags_batch(files)
        return [cls(f, tags=by_file.get(os.path.normpath(f), {})) for f in files]

    def __init__(self, file, tags):
        self.file = file
        self.ctx = Context.get()

        self.tags = tags

        self.sequence = None
        if PictureInfo.T_SEQUENCE_NUMBER in self.tags and self.tags[PictureInfo.T_SEQUENCE_NUMBER] > 0:
            self.sequence = self.tags[PictureInfo.T_SEQUENCE_NUMBER]

        self.cluster = None
        self.get_place_name = None

        _dirname, _basename = os.path.split(self.file)
        self.dirname = _dirname
        self.filename = _basename
        self.filename_root, _ = os.path.splitext(_basename)

    @property
    def accessory_files(self):
        """The sidecars sitting next to the picture, by filename root.

        Listed on demand: scanning a whole shoot would otherwise read every
        directory once per file in it.
        """
        return [os.path.join(self.dirname, f) for f in os.listdir(self.dirname)
                if f.startswith(self.filename_root) and f != self.filename]

    def get_date_time(self):
        return self.tags[PictureInfo.T_DATE_TIME_ORIGINAL]

    def has_date_time(self):
        return PictureInfo.T_DATE_TIME_ORIGINAL in self.tags

    def get_utc_offset(self):
        """The camera's ``OffsetTimeOriginal`` as a timezone, or None."""
        return self.tags.get(PictureInfo.T_OFFSET_TIME_ORIGINAL)

    def get_utc_datetime(self):
        """``UTC = DateTimeOriginal - OffsetTimeOriginal``.

        Returns None when either tag is missing, since the camera then gives
        no way to place the shot on the absolute timeline a GPS track uses.
        """
        offset = self.get_utc_offset()
        if offset is None or not self.has_date_time():
            return None

        return self.get_date_time().replace(tzinfo=offset).astimezone(timezone.utc)

    def get_sequence_number(self):
        return self.sequence

    def has_latlon(self):
        return PictureInfo.T_GPS_LATITUDE in self.tags and PictureInfo.T_GPS_LONGITUDE in self.tags

    def get_latlon(self):
        return (self.tags[PictureInfo.T_GPS_LATITUDE],
                self.tags[PictureInfo.T_GPS_LONGITUDE]) if self.has_latlon() else None

    def get_distance(self, latlon1):
        return haversine(self.get_latlon(), latlon1) if self.has_latlon() else None

    def move_files(self, dst_folder, dst_filename_root):
        from os.path import basename, exists, join, split

        def new_dest_file(f):
            old_folder, old_basename = split(f)
            new_basename = re.sub(f'^{re.escape(self.filename_root)}',
                                  dst_filename_root,
                                  old_basename,
                                  flags=re.IGNORECASE)
            folder = old_folder if self.ctx.file_settings.rename_only else dst_folder
            return join(folder, new_basename)

        def change_xmp():
            dst_ = dst + '.tmp'
            # Writes the xmp content with updated filename references
            with open(dst, 'r', encoding='utf-8') as i, open(dst_, 'w', encoding='utf-8') as o:
                for line in i.readlines():
                    if self.filename in line:
                        line = line.replace(self.filename, new_filename)
                    o.write(line)

            # Replace the original XMP file with the modified one
            shutil.move(dst_, dst)

        old_files = [self.file] + self.accessory_files
        new_files = [new_dest_file(f) for f in old_files]

        if old_files[0] == new_files[0]:
            logging.warning(f"Skipping renaming of {self.file}, as it's the same destination")
            return []

        # check beforehand if any destination file exists
        for _f in new_files:
            if exists(_f):
                raise FileExistsError(f"File {basename(_f)} already exists")

        # move all the files and change the content of the xmp sidecars file
        for src, dst in zip(old_files, new_files):
            if src == self.file:
                new_filename = basename(dst)

            logging.debug(f"{src[len(self.ctx.file_settings.search_dir):]} -> {dst}")
            if not self.ctx.file_settings.dry_run:
                shutil.move(src, dst)

            if dst.lower().endswith('.xmp'):
                logging.debug(f" > Updating XMP sidecar for {basename(dst)}")
                if not self.ctx.file_settings.dry_run:
                    # replace old filename inside XMP sidecar
                    change_xmp()

        return new_files

    def __str__(self):
        return (f"File: {os.path.basename(self.file)}; "
                f"Sequence: {self.sequence}; "
                f"Geolocation: {'yes' if self.has_latlon() else 'no'};")

    def __hash__(self):
        return hash(self.file)

    def __eq__(self, other):
        return self.file == other.file


class PictureCluster:
    def __init__(self, name, first_picture: PictureInfo = None):
        self.ctx = Context.get()
        self.name = name
        self.center = (0, 0)
        self.radius = 0
        self.pictures: set[PictureInfo] = set()
        if first_picture:
            self.add_picture(first_picture)

    def is_in_range(self, picture: PictureInfo) -> bool:
        return picture.get_distance(self.center) < max(self.ctx.location_settings.search_radius, self.radius)

    def contains(self, picture: PictureInfo) -> bool:
        return picture in self.pictures

    def add_picture(self, picture: PictureInfo):
        self.pictures.add(picture)
        picture.cluster = self

        # compute new center
        _new_center = reduce(lambda l1, l2: (l1[0] + l2[0], l1[1] + l2[1]), [i.get_latlon() for i in self.pictures])
        _new_center = (_new_center[0] / len(self.pictures), _new_center[1] / len(self.pictures))
        self.center = _new_center

        # compute new statistical circle (to cover the area of all pictures)

        if len(self.pictures) == 1:
            self.radius = self.ctx.location_settings.search_radius / 10
        else:
            # recompute distances from center
            _distances = [haversine(self.center, p.get_latlon()) for p in self.pictures]

            # Use 2 Standard Deviations for the radius (covers ~95% of points)
            # This ignores extreme outliers but covers the main group well
            _avg_dist = sum(_distances) / len(_distances)
            _variance = sum((d - _avg_dist) ** 2 for d in _distances) / len(_distances)
            _std_dev = sqrt(_variance)

            # Radius = Average Distance + 2 * Standard Deviation
            # This creates a circle that 'approximately' covers everything.
            self.radius = _avg_dist + (2 * _std_dev)
