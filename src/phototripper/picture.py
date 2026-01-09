import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from functools import reduce
from math import sqrt

from .common import haversine, Context


class PictureInfo:
    T_DATE_TIME_ORIGINAL = "DateTimeOriginal"
    T_SEQUENCE_NUMBER = "SequenceNumber"
    T_GPS_LATITUDE = "GPSLatitude"
    T_GPS_LONGITUDE = "GPSLongitude"

    TAGS = {
        T_DATE_TIME_ORIGINAL: lambda x: datetime.strptime(x, "%Y:%m:%d %H:%M:%S"),
        T_SEQUENCE_NUMBER: lambda x: int(x),
        T_GPS_LATITUDE: lambda x: float(x),
        T_GPS_LONGITUDE: lambda x: float(x),
    }

    TAG_RE = re.compile(r"(?P<tag>\w+)\s*:\s*(?P<value>.+)$")

    def __init__(self, file):
        self.file = file
        self.ctx = Context.get()

        # exiftool -n -DateTimeOriginal -SequenceNumber -GPSLatitude -GPSLongitude
        out = subprocess.check_output(["exiftool", "-n", "-s",
                                       *['-' + t for t in PictureInfo.TAGS.keys()],
                                       file])

        self.tags = {}
        for line in out.decode('utf-8').splitlines():
            tag, value = PictureInfo.TAG_RE.search(line).groups()
            self.tags[tag] = PictureInfo.TAGS[tag](value)

        self.sequence = None
        if PictureInfo.T_SEQUENCE_NUMBER in self.tags and self.tags[PictureInfo.T_SEQUENCE_NUMBER] > 0:
            self.sequence = self.tags[PictureInfo.T_SEQUENCE_NUMBER]

        self.cluster = None
        self.get_place_name = None

        _dirname, _basename = os.path.split(self.file)
        self.filename = _basename
        self.filename_root, _ = os.path.splitext(_basename)
        self.accessory_files = [os.path.join(_dirname, f) for f in os.listdir(_dirname)
                                if f.startswith(self.filename_root) and f != _basename]

    def get_date_time(self):
        return self.tags[PictureInfo.T_DATE_TIME_ORIGINAL]

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
        from os.path import join, exists, basename, split

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
