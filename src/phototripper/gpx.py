import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import namedtuple
from datetime import timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import tabulate

from .common import (
    Context,
    FileSettings,
    GpxSettings,
    LoggingSettings,
    discover_files,
    register_defaults,
    resolve_target,
    setup_logging,
)
from .picture import PictureInfo, format_utc_offset
from .track import (
    BEFORE,
    DEFAULT_MAX_EXT_SECS,
    DEFAULT_MAX_INT_SECS,
    INSIDE,
    GpsTrack,
    resolve_track_files,
)

register_defaults(
    "gpx",
    {
        "track": None,
        "max-int-secs": None,
        "max-ext-secs": None,
        "geosync": None,
        "sidecar": False,
        "overwrite-gps": False,
        "skip-dst-check": False,
    },
    bools={"sidecar", "overwrite-gps", "skip-dst-check"},
    ints={"max-int-secs", "max-ext-secs"},
)

# Why a picture cannot be geotagged as things stand. Each carries the setting
# that would fix it, so the report always tells the user what to do next.
Issue = namedtuple("Issue", "key, description, hint")

I_HAS_GPS = Issue(
    'has-gps',
    'already geotagged',
    'use --overwrite-gps to replace the existing coordinates')
I_HAS_SIDECAR = Issue(
    'has-sidecar',
    'already has an XMP sidecar',
    'use --overwrite-gps to update the existing sidecar')
I_NO_DATETIME = Issue(
    'no-datetime',
    'no DateTimeOriginal',
    'the camera recorded no shooting time; nothing can match it to the track')
I_NO_OFFSET = Issue(
    'no-offset',
    'no OffsetTimeOriginal',
    'the camera recorded no UTC offset; fix the time offset before geotagging')
I_GAP = Issue(
    'gap',
    'falls in a gap in the track',
    'raise the interpolation tolerance')
I_BEFORE = Issue(
    'before-track',
    'shot before the track starts',
    'raise the extrapolation tolerance')
I_AFTER = Issue(
    'after-track',
    'shot after the track ends',
    'raise the extrapolation tolerance')

ISSUE_ORDER = [I_GAP, I_BEFORE, I_AFTER, I_NO_OFFSET, I_NO_DATETIME,
               I_HAS_GPS, I_HAS_SIDECAR]

SIDECAR_EXT = '.xmp' 

# A picture and the verdict of the pre-flight analysis. *issue* is None when
# the picture can be geotagged as is.
Candidate = namedtuple("Candidate", "picture, utc, issue, required_secs, latlon")

ReportRow = namedtuple("ReportRow", "issue, files, worst, suggestion")

# A day on which the camera's UTC offset disagrees with the one in force where
# the track says the pictures were taken.
DstRow = namedtuple("DstRow", "dates, zone, camera, expected, files")

# how far apart two points may be and still share a time zone lookup
GEO_ROUNDING = 1

# exiftool names every file it could not place; the pre-flight analysis should
# have caught them all, so anything here is worth reporting back.
UNTAGGED_RE = re.compile(r"^Warning: No writable tags set from (?P<file>.+)$")

# ...and it counts its own work, which beats inferring the tally ourselves
WRITTEN_RE = re.compile(r"^(?P<count>\d+) image files (?:updated|created)$")
FAILED_RE = re.compile(r"^(?P<count>\d+) files weren't updated due to errors$")


def register_args(subparsers):
    parser = subparsers.add_parser(
        'gpx', help='Geotag pictures from a GPS track')

    file_group = parser.add_argument_group('File options')
    file_group.add_argument('-s', "--search-dir",
                            help="Directory to scan, or a single picture")
    file_group.add_argument('-t', "--track",
                            help="GPS track: a .gpx file, a comma separated list "
                                 "of them, or a folder holding them")
    file_group.add_argument('--filter', help="Filter files by substring match")
    file_group.add_argument("--recursive", action='store_true', default=None,
                            help="Scan recursively files from root directory")

    tag_group = parser.add_argument_group('Geotagging options')
    tag_group.add_argument("--sidecar", action='store_true', default=None,
                           help="Write an XMP sidecar instead of the original file")
    tag_group.add_argument("--overwrite-gps", action='store_true', default=None,
                           help="Re-tag pictures that already carry coordinates")
    tag_group.add_argument("--max-int-secs", type=int, default=None,
                           help=f"Largest gap in the track to interpolate across, "
                                f"in seconds (exiftool default {DEFAULT_MAX_INT_SECS})")
    tag_group.add_argument("--max-ext-secs", type=int, default=None,
                           help=f"How far beyond either end of the track to "
                                f"extrapolate, in seconds "
                                f"(exiftool default {DEFAULT_MAX_EXT_SECS})")
    tag_group.add_argument("--geosync", default=None,
                           help="Correct a camera clock drift, as exiftool -geosync")
    tag_group.add_argument("--skip-dst-check", action='store_true', default=None,
                           help="Don't check the camera offset against the "
                                "local time zone")

    log_group = parser.add_argument_group('Logging options')
    log_group.add_argument("--verbose", action='store_true', default=None,
                           help="Logs more")
    log_group.add_argument('--summary', action='store_true', default=None,
                           help="Show summary")
    log_group.add_argument("--dry-run", action='store_true', default=None,
                           help="Only analyse and report, write nothing")

    parser.set_defaults(func=run)


DAY_SECS = 24 * 3600

# Past this the pictures and the track simply don't describe the same outing,
# and suggesting a tolerance that wide would be bad advice rather than a fix.
IMPLAUSIBLE_SECS = DAY_SECS


def _offset_hours(text):
    """Hours in an offset such as ``+02:00``, signed."""
    sign = -1 if text.startswith('-') else 1
    hours, minutes = (int(part) for part in text[1:].split(':'))
    return sign * (hours + minutes / 60)


def resolve_timezones(latlons):
    """Name the time zone of each point, offline.

    exiftool ships a geolocation database, so this costs no API key and no
    network. Every point goes in one invocation through an argument file.
    """
    if not latlons:
        return {}

    directives = []
    for lat, lon in latlons:
        directives += ["-api", f"geolocation={lat},{lon}",
                       "-p", "$GeolocationTimeZone", "-f", "-execute"]

    with tempfile.NamedTemporaryFile('w', suffix='.args', delete=False) as arg_file:
        arg_file.write("\n".join(directives))
        arg_file_name = arg_file.name

    try:
        result = subprocess.run(["exiftool", "-@", arg_file_name],
                                capture_output=True, text=True)
    finally:
        os.unlink(arg_file_name)

    zones = {}
    for latlon, line in zip(latlons, result.stdout.splitlines()):
        zone = line.strip()
        if zone and zone != '-':
            zones[latlon] = zone

    return zones


def sidecar_of(picture):
    """Where the XMP sidecar of *picture* lives."""
    return os.path.join(picture.dirname, picture.filename_root + SIDECAR_EXT)


def _or_default(value, fallback):
    """exiftool applies its own default when a tolerance is left unset."""
    return fallback if value is None else value


def humanize(seconds):
    """Render a tolerance the way a person reads a duration."""
    seconds = int(math.ceil(seconds))

    if seconds < 60:
        return f"{seconds} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * DAY_SECS:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / DAY_SECS:.1f} days"


def run(args):
    def check():
        logging.debug("Checking pre-requisites...")
        if not shutil.which("exiftool"):
            raise ValueError("Exiftool is not found in this system")

        if not args.track:
            raise ValueError(
                "No GPS track specified: use --track, or set 'track' in the "
                "[gpx] section of phototripper.ini")

    def initialize_context():
        gpx_settings = GpxSettings(
            track_files,
            _or_default(args.max_int_secs, DEFAULT_MAX_INT_SECS),
            _or_default(args.max_ext_secs, DEFAULT_MAX_EXT_SECS),
            args.geosync,
            args.sidecar,
            args.overwrite_gps,
            args.skip_dst_check,
        )

        _ctx = Context(
            None,  # gpx never asks a location service anything
            LoggingSettings(args.verbose, args.summary, False),
            FileSettings(search_dir, args.recursive, None,
                         args.dry_run, False, None),
            gpx_settings=gpx_settings,
        )

        Context.set(_ctx)
        return _ctx

    def collect():
        logging.info("Collecting pictures...")
        files = discover_files(args.search_dir, args.recursive, args.filter)

        if not files:
            logging.warning("No pictures found to geotag.")

        return PictureInfo.scan(files)

    def read_track():
        logging.info(f"Reading {len(track_files)} track file(s)...")
        for file in track_files:
            logging.info(f" > {file}")

        _track = GpsTrack.read(track_files)

        if not len(_track):
            raise ValueError(
                "The track holds no timestamped points, so nothing can be matched "
                "to it. Check that the GPS logger recorded times.")

        if _track.undated:
            logging.warning(
                f"Ignored {_track.undated} track point(s) with no timestamp.")

        logging.info(
            f"Track covers {_track.start:%Y-%m-%d %H:%M:%S} to "
            f"{_track.end:%Y-%m-%d %H:%M:%S} UTC ({len(_track)} points)")

        return _track

    def analyse():
        """Decide, for every picture, whether the track can place it."""
        settings = context.gpx_settings
        results = []

        for picture in pictures:
            if not picture.has_date_time():
                results.append(Candidate(picture, None, I_NO_DATETIME, None, None))
                continue

            utc = picture.get_utc_datetime()
            if utc is None:
                results.append(Candidate(picture, None, I_NO_OFFSET, None, None))
                continue

            coverage = track.locate(utc)

            # the time is always recorded, even for a picture kept back below,
            # so the report can tell a timeline mismatch from a plain skip
            if coverage.position == INSIDE:
                covered = coverage.required_secs <= settings.max_int_secs
                issue = None if covered else I_GAP
            else:
                covered = coverage.required_secs <= settings.max_ext_secs
                issue = None if covered else (
                    I_BEFORE if coverage.position == BEFORE else I_AFTER)

            if not settings.overwrite_gps:
                # never replace what the pictures already carry
                if picture.has_latlon():
                    issue = I_HAS_GPS
                elif settings.sidecar and os.path.exists(sidecar_of(picture)):
                    issue = I_HAS_SIDECAR

            results.append(Candidate(picture, utc, issue,
                                     coverage.required_secs, coverage.latlon))

        return results

    def report():
        """Print what stands in the way, and the setting that removes it."""
        by_issue = {}
        for candidate in candidates:
            if candidate.issue:
                by_issue.setdefault(candidate.issue, []).append(candidate)

        if not by_issue:
            logging.info(f"All {len(candidates)} picture(s) can be geotagged.")
            return

        rows = []
        for issue in ISSUE_ORDER:
            affected = by_issue.get(issue)
            if not affected:
                continue

            worst = '-'
            suggestion = issue.hint

            if issue in (I_GAP, I_BEFORE, I_AFTER):
                needed = int(math.ceil(max(c.required_secs for c in affected)))
                option = '--max-int-secs' if issue is I_GAP else '--max-ext-secs'
                worst = humanize(needed)
                suggestion = (
                    f"{option} {needed}" if needed <= IMPLAUSIBLE_SECS
                    else "too far off to be a tolerance problem; see below")

            rows.append(ReportRow(issue.key, len(affected), worst, suggestion))

            for candidate in affected:
                logging.debug(f" > {candidate.picture.filename}: {issue.description}")

        logging.info("\nIssues -----------------------------------------------")
        logging.info(tabulate.tabulate(rows, headers=ReportRow._fields,
                                       tablefmt='pipe'))
        logging.info(f"\n{len(taggable)} of {len(candidates)} picture(s) "
                     f"can be geotagged as things stand.")
        logging.info(
            "\nA suggestion can also go in the [gpx] section of phototripper.ini, "
            "without the leading dashes.")

        explain_total_miss()

    def explain_total_miss():
        """Say why the timelines don't meet, when they plainly don't.

        Only worth saying when every picture carrying a usable time sits more
        than a day away from the track: anything closer is an ordinary
        tolerance question, which the issue table already answers.
        """
        def distance_from_track(utc):
            if utc < track.start:
                return (track.start - utc).total_seconds()
            if utc > track.end:
                return (utc - track.end).total_seconds()
            return 0

        timed = [c for c in candidates if c.utc]
        if not timed or any(distance_from_track(c.utc) <= IMPLAUSIBLE_SECS
                            for c in timed):
            return

        moments = [c.utc for c in timed]

        logging.warning(
            f"\nNot a single picture falls near the track.\n"
            f"  pictures span {min(moments):%Y-%m-%d %H:%M:%S} to "
            f"{max(moments):%Y-%m-%d %H:%M:%S} UTC\n"
            f"  track spans    {track.start:%Y-%m-%d %H:%M:%S} to "
            f"{track.end:%Y-%m-%d %H:%M:%S} UTC\n"
            f"Check that this is the right track, and that the camera's "
            f"OffsetTimeOriginal is right -- a wrong offset shifts every "
            f"picture by whole hours.")

    def dst_check():
        """Compare the camera's UTC offset with the one in force where we were.

        A camera left on winter time through a summer trip still geotags
        correctly, because DateTimeOriginal and OffsetTimeOriginal move
        together and the UTC they derive is right either way. What it gets
        wrong is the time it displays, which is what sort names folders by --
        so this is worth saying, and worth saying separately.
        """
        checkable = [c for c in candidates
                     if c.utc and c.latlon and c.picture.get_utc_offset()]
        if not checkable:
            return

        cells = sorted({(round(c.latlon[0], GEO_ROUNDING),
                         round(c.latlon[1], GEO_ROUNDING)) for c in checkable})

        logging.debug(f"Resolving {len(cells)} location(s) to a time zone...")
        zones = resolve_timezones(cells)

        if not zones:
            logging.info(
                "Skipping the camera time check: this exiftool build has no "
                "geolocation database to name the time zone from.")
            return

        mismatches = {}
        for candidate in checkable:
            cell = (round(candidate.latlon[0], GEO_ROUNDING),
                    round(candidate.latlon[1], GEO_ROUNDING))
            if cell not in zones:
                continue

            zone = zones[cell]
            try:
                local = candidate.utc.astimezone(ZoneInfo(zone))
            except ZoneInfoNotFoundError:
                logging.debug(f" > no time zone data for {zone}")
                continue

            expected = local.utcoffset()
            camera = candidate.picture.get_utc_offset().utcoffset(None)
            if camera == expected:
                continue

            mismatches.setdefault((zone, camera, expected), []).append(local.date())

        if mismatches:
            report_dst(mismatches)
        else:
            logging.debug(" > the camera's UTC offset matches every location")

    def report_dst(mismatches):
        rows = []
        for (zone, camera, expected), dates in sorted(mismatches.items()):
            span = f"{min(dates)}" if min(dates) == max(dates) else \
                   f"{min(dates)} .. {max(dates)}"
            rows.append(DstRow(span, zone,
                               format_utc_offset(timezone(camera)),
                               format_utc_offset(timezone(expected)),
                               len(dates)))

        logging.warning(
            "\nCamera time -------------------------------------------")
        logging.warning(tabulate.tabulate(rows, headers=DstRow._fields,
                                          tablefmt='pipe'))

        for row in rows:
            shift = _offset_hours(row.expected) - _offset_hours(row.camera)
            sign = '+' if shift > 0 else '-'

            if abs(shift) == 1:
                switched = 'off' if shift > 0 else 'on'
                logging.warning(
                    f"\n{row.zone}: the camera says {row.camera} but "
                    f"{row.expected} was in force -- daylight saving looks "
                    f"like it was left switched {switched}.")
            else:
                logging.warning(
                    f"\n{row.zone}: the camera says {row.camera} but "
                    f"{row.expected} was in force.")

            # EXIF cannot tell the two apart, so name both and let the user
            # say which one happened
            logging.warning(
                f"  If the clock was set to match the offset, the coordinates "
                f"are right and only the displayed time is out:\n"
                f"    exiftool -P -overwrite_original "
                f"-AllDates{sign}={abs(shift):g} "
                f"-OffsetTimeOriginal={row.expected} <the pictures of "
                f"{row.dates}>\n"
                f"  If instead the clock was right and only the offset wrong, "
                f"these pictures are geotagged {abs(shift):g}h off: correct "
                f"OffsetTimeOriginal and geotag them again with "
                f"--overwrite-gps.")

    def print_summary():
        logging.info(
            "\n============================================================")
        logging.info(context.to_summary())

        offsets = {format_utc_offset(p.get_utc_offset())
                   for p in pictures if p.get_utc_offset()}

        logging.info("Pictures:")
        logging.info(f"    Found                   : {len(pictures)}")
        logging.info(f"    Ready to geotag         : {len(taggable)}")
        listed = ', '.join(sorted(offsets)) or 'none'
        logging.info(f"    Camera UTC offsets      : {listed}")

    def build_command():
        """The exiftool invocation that writes the coordinates."""
        settings = context.gpx_settings

        command = ["exiftool", "-P"]

        command.append("-overwrite_original")

        if settings.sidecar:
            # writes <name>.xmp beside the picture, leaving the RAW untouched.
            # -srcfile reads the times from the picture but writes to the
            # sidecar, creating it or updating one that is already there.
            command += ["-srcfile", f"%d%f{SIDECAR_EXT}"]

        if args.max_int_secs is not None:
            command += ["-api", f"GeoMaxIntSecs={settings.max_int_secs}"]
        if args.max_ext_secs is not None:
            command += ["-api", f"GeoMaxExtSecs={settings.max_ext_secs}"]
        if settings.geosync:
            command += ["-geosync", settings.geosync]

        for track_file in settings.track_files:
            command += ["-geotag", track_file]

        # UTC = DateTimeOriginal - OffsetTimeOriginal
        command.append("-geotime<${DateTimeOriginal}${OffsetTimeOriginal}")

        return command

    def geotag():
        """Write the coordinates, for the pictures the track can place."""
        if not taggable:
            logging.warning("Nothing to geotag.")
            return

        target = "XMP sidecars" if context.gpx_settings.sidecar else "original files"
        logging.info(f"\nGeotagging {len(taggable)} picture(s), writing to {target}...")

        with tempfile.NamedTemporaryFile('w', suffix='.args', delete=False) as arg_file:
            arg_file.write("\n".join(c.picture.file for c in taggable))
            arg_file_name = arg_file.name

        try:
            result = subprocess.run([*build_command(), "-@", arg_file_name],
                                    capture_output=True, text=True)
        finally:
            os.unlink(arg_file_name)

        report_written(result)

    def report_written(result):
        """Say what exiftool did, and name anything it refused to place."""
        refused = []
        problems = []

        for line in (line.strip() for line in result.stderr.splitlines()):
            if not line:
                continue

            match = UNTAGGED_RE.match(line)
            if match:
                refused.append(os.path.basename(match.group('file')))
            else:
                problems.append(line)
                logging.debug(f" > {line}")

        written = failed = 0
        for line in (line.strip() for line in result.stdout.splitlines()):
            if not line:
                continue

            logging.debug(f" > {line}")

            match = WRITTEN_RE.match(line)
            if match:
                written += int(match.group('count'))
            match = FAILED_RE.match(line)
            if match:
                failed += int(match.group('count'))

        skipped = len(candidates) - len(taggable)
        logging.info(f"{written} geotagged, {skipped} skipped, {failed} failed")

        if refused:
            logging.warning(
                "exiftool could not place these, although the analysis expected "
                "it to: " + ", ".join(refused))

        if failed and problems:
            logging.warning("exiftool reported: " + "; ".join(problems[:5]))

        if result.returncode != 0 and not (refused or failed):
            raise ValueError(f"exiftool failed: {result.stderr.strip()}")

    setup_logging(args.verbose)

    check()

    search_dir = resolve_target(args.search_dir)[0]
    track_files = resolve_track_files(args.track)

    context: Context = initialize_context()

    start_time = time.perf_counter()

    track = read_track()
    pictures = collect()
    candidates = analyse()
    taggable = [c for c in candidates if c.issue is None]

    report()

    if not context.gpx_settings.skip_dst_check:
        dst_check()

    if args.dry_run:
        logging.info("\nDry run: nothing was written.")
    else:
        geotag()

    if args.summary:
        print_summary()

    logging.info(f"Done in {time.perf_counter() - start_time:.3f} seconds")
