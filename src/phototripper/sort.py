import logging
import os
import re
import shutil
import tempfile
import time
from collections import namedtuple
from datetime import datetime
from math import cos, radians, sin

import googlemaps
import tabulate
from unidecode import unidecode

from .common import (
    Context,
    FileSettings,
    LocationSettings,
    LoggingSettings,
    get_google_api_key,
)
from .location import GeoTraits, PictureLocation
from .picture import PictureCluster, PictureInfo

# TODO: option for replacement
# TODO: option for running exiftool and adding GPS coordinates before scanning
# TODO: configuration file for common options

SUPPORTED_RAW_EXT = ["arw"]

VARS_RE = re.compile(r"(?P<prefix>[\s\-_]+)?(\{(?P<var>\w+)(:(?P<format>\w+))?})")

VARS = {
    'datetime': lambda pic, fmt: datetime.strftime(pic.get_date_time(), fmt),
    'date': lambda pic, fmt: datetime.strftime(pic.get_date_time(), fmt),
    'time': lambda pic, fmt: datetime.strftime(pic.get_date_time(), fmt),
    'day': lambda pic, _=None: datetime.strftime(pic.get_date_time(), "%d"),
    'month': lambda pic, fmt: datetime.strftime(pic.get_date_time(), fmt),
    'year': lambda pic, _=None: datetime.strftime(pic.get_date_time(), "%Y"),
    'sequence': lambda pic,
                       repl: None if pic.get_sequence_number() is None and repl is None else f"{pic.get_sequence_number() or repl:02d}",
    'place': lambda pic, _=None: pic.get_place_name(),
}

VARS_FORMAT = {
    'datetime': {
        'extended': "%Y-%m-%dT%H:%M:%S",
        'compact': "%Y%m%dT%H%M%S",
        '$default': 'compact'
    },
    'date': {
        'extended': "%Y-%m-%d",
        'compact': "%Y%m%d",
        '$default': 'compact'
    },
    'time': {
        'extended': "%H:%M:%S",
        'compact': "%H%M%S",
        '$default': 'compact'
    },
    'month': {
        'number': "%m",
        'name': "%B",
        '$default': 'number'
    },
    'sequence': {
        'none': None,
        'zero': 0,
        '$default': 'none'
    },
}

SummaryRow = namedtuple("SummaryRow", 'file, new_file, date, cluster, place, moved_files')


def _traits_to_geojson(cluster, traits):
    num_circle_points = 64
    # 1 deg lat is approx 111,320 meters
    meters_per_deg_lat = 111320

    def rect_feature(_geotraits: GeoTraits):
        ne_lat, ne_lon = _geotraits.viewport[0]
        sw_lat, sw_lon = _geotraits.viewport[1]

        # --- 1. Generate Rectangle Coordinates ---
        # GeoJSON requires [lon, lat] order and must close the loop (start == end)
        rect_coords = [[
            [sw_lon, sw_lat],  # SW
            [ne_lon, sw_lat],  # SE
            [ne_lon, ne_lat],  # NE
            [sw_lon, ne_lat],  # NW
            [sw_lon, sw_lat]  # Close loop
        ]]

        return {
            "type": "Feature",
            "properties": {
                "name": _geotraits.display_name,
                "description":
                    f"Size factor: {_geotraits.intersection_analysis.get_size_factor():.5f}; "
                    f"Size ratio: {_geotraits.intersection_analysis.get_size_ratio():.5f}; "
                    f"Center factor: {_geotraits.intersection_analysis.get_center_factor():.5f}",
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": rect_coords
            }
        }

    def circle_feature():
        # Unpack coordinates (lat, lon)
        c_lat, c_lon = cluster.center
        # 1 deg lon depends on latitude: 111,320 * cos(lat)
        meters_per_deg_lon = 111320 * cos(radians(c_lat))

        circle_coords_ring = []
        for i in range(num_circle_points + 1):
            # Calculate angle in radians
            theta = radians(i * (360 / num_circle_points))

            # Calculate offset in degrees
            dx = (cluster.radius * cos(theta)) / meters_per_deg_lon
            dy = (cluster.radius * sin(theta)) / meters_per_deg_lat

            circle_coords_ring.append([c_lon + dx, c_lat + dy])

        return {
            "type": "Feature",
            "properties": {
                "name": f"Supposedly {cluster.name}",
                "radius_meters": cluster.radius,
                "center": [cluster.center[0], cluster.center[1]],
            },
            "geometry": {
                "type": "Polygon",
                "coordinates": [circle_coords_ring],
            }
        }

    return {
        "type": "FeatureCollection",
        "features": [
            circle_feature(),
            *[rect_feature(geotrait) for geotrait in traits]
        ]
    }


def _get_new_name(template, pic: PictureInfo):
    m = VARS_RE.search(template)
    while m:
        _var = m.group('var')
        _fmt = m.group('format')
        _prefix = m.group('prefix')

        if _var in VARS:
            if _fmt:
                if _var in VARS_FORMAT and _fmt in VARS_FORMAT[_var]:
                    _fmt = VARS_FORMAT[_var][_fmt]
                else:
                    raise ValueError(f"Unknown format {_fmt} for variable {_var}")
            else:
                if _var in VARS_FORMAT:
                    _fmt = VARS_FORMAT[_var][VARS_FORMAT[_var]['$default']]

            replacement = VARS[_var](pic, _fmt)
            if replacement:
                template = template.replace(m.group(0), f"{_prefix if _prefix else ''}{replacement}", 1)
            else:
                template = template.replace(m.group(0), "", 1)
        else:
            raise ValueError(f"Unknown variable: {_var}")

        m = VARS_RE.search(template)

    return template


def _vvv_fff(_v):
    _f = None
    if _v in VARS_FORMAT:
        _df = VARS_FORMAT[_v]['$default']
        _f = ', '.join(['*' + _df] + [f"{_fk}{'*' if _df == _fk else ''}"
                                       for _fk in VARS_FORMAT[_v].keys()
                                       if _fk not in ('$default', _df)])

    if _f:
        return f"'{_v}' ({_f})"
    else:
        return f"'{_v}'"


def register_args(subparsers):
    parser = subparsers.add_parser('sort', help='Sort and rename photo files')

    file_group = parser.add_argument_group('File options')
    file_group.add_argument('-s', "--search-dir", help="Search directory")
    file_group.add_argument('-d', "--destination", help="Destination directory")
    file_group.add_argument('--filter', help="Filter files by substring match")
    file_group.add_argument("--recursive", action='store_true', default=None, help="Scan recursively files from root directory")
    file_group.add_argument("--rename-only", action='store_true', default=None, help="Only renames files (without moving them)")
    file_group.add_argument('-f', '--rename-format', default=None,
                            help=f"""
Renames files according to the specified format, using substitution variables with syntax '{{<var>[:<format>]}}'.
A slash will create a folder.
Any dash, space or underscore before the variable will be stripped away if the variable can't be retrieved.
The following variables are supported (star=default): {", ".join([_vvv_fff(_v) for _v in VARS.keys()])}.
Eg: '{{year}}/{{month}} - {{month:name}}/{{day}} - {{place}}/IMG_{{datetime}}_{{sequence}}' -->
'2023/03 - March/15 - New York/IMG_20230315T143000_05'.
Default is '{{day}} - {{place}}/IMG_{{datetime:extended}}_{{sequence}}'""")

    log_group = parser.add_argument_group('Logging options')
    log_group.add_argument("--verbose", action='store_true', default=None, help="Logs more")
    log_group.add_argument('--summary', action='store_true', default=None, help="Show summary")
    log_group.add_argument("--dry-run", action='store_true', default=None, help="Don't move/rename files")

    loc_group = parser.add_argument_group('Location service')
    loc_group.add_argument("--skip-location", action='store_true', default=None, help="Don't use location services")
    loc_group.add_argument("--search-radius", help="Search radius", type=int, default=None)
    loc_group.add_argument("--cache", help="JSON service cache folder (default is temp folder)", default=None)
    loc_group.add_argument("--debug", action='store_true', default=None, help="Debug geocoding decisions")

    parser.set_defaults(func=run)


def run(args):
    def collect():
        def add_picture_file(picture_file):
            logging.debug(f"Adding picture file {os.path.basename(picture_file)}")
            picture = PictureInfo(picture_file)

            all_pictures.append(picture)

            if picture.has_latlon():
                cluster_found = False
                for cluster in geo_clusters:
                    if cluster.is_in_range(picture):
                        cluster.add_picture(picture)
                        logging.debug(f" > picture falls in cluster {cluster.name} "
                                      f"with center {cluster.center} "
                                      f"together with {len(cluster.pictures)} pictures")
                        cluster_found = True
                        break

                if not cluster_found:
                    cluster = PictureCluster(f"cluster{len(geo_clusters) + 1}", picture)
                    geo_clusters.append(cluster)
                    logging.debug(f" > created new cluster {cluster.name} with center {cluster.center}")

        # collects all pictures and read their properties
        logging.info("Collecting pictures...")
        for _root, _dirs, _files in os.walk(search_dir, topdown=True):
            logging.info(f"Scanning {_root} ({len(_files)} files)...")
            _dirs.sort()
            _files.sort()
            for _pic_file in [os.path.join(_root, f) for f in _files if f[-3:].lower() in SUPPORTED_RAW_EXT]:
                if not args.filter or args.filter in _pic_file:
                    logging.debug(f" > {_pic_file[len(_root):]}")
                    add_picture_file(_pic_file)

            if not args.recursive:
                break

    def move():
        def place_none():
            return None

        def place_full_strategy():
            traits = [
                _location.first_by_service('gplaces'),
                _location.first_by_service('geocode'),
                _location.first_by_score(use_center_factor=True, use_size_factor=False)
            ]

            _places = list(dict.fromkeys([unidecode(t.get_place_name()) for t in traits if t]))
            return ", ".join(_places)

        _pict_counter = _checkpoint_counter = 0
        _checkpoint_start = time.perf_counter()

        logging.info(f"Loading pictures metadata with location strategy {context.location_settings.strategy}...")
        for info in all_pictures:
            _pict_counter += 1
            _checkpoint_counter += 1
            if _checkpoint_counter > 50 and time.perf_counter() - _checkpoint_start > 10:
                _checkpoint_start = time.perf_counter()
                _checkpoint_counter = 0
                logging.info(f"Processing pictures ({_pict_counter}/{len(all_pictures)})...")

            if info.cluster is None or context.location_settings.strategy == 'none':
                info.get_place_name = place_none
            else:
                _location = next((_l for _l in locations if _l.cluster == info.cluster), None)
                if _location is None:
                    _location = PictureLocation(info.cluster)
                    locations.append(_location)
                info.get_place_name = place_full_strategy

            destination = os.path.join(dest_dir, _get_new_name(context.file_settings.rename_pattern, info))
            destination_folder, destination_basename = os.path.split(destination)

            if not args.dry_run:
                os.makedirs(destination_folder, exist_ok=True)

            moved_files = info.move_files(destination_folder, destination_basename)

            summary_rows.append(
                SummaryRow(
                    info.file[len(search_dir):],
                    destination[len(dest_dir):],
                    info.get_date_time().strftime("%Y-%m-%d %H:%M:%S"),
                    info.cluster.name if info.cluster else None,
                    info.get_place_name(),
                    len(moved_files)))

    def print_summary():
        logging.info(
            '\nSummary ------------------------------------------------------------------------------------------')
        logging.info(
            tabulate.tabulate(
                summary_rows,
                headers=SummaryRow._fields,
                tablefmt='pipe'))

        SubSummaryRow = namedtuple('SubSummaryRow', 'cluster_name, place, date, moved_files')
        sub_summary_rows = []

        _summary_recap = {}
        _cluster_to_place = {}

        for summary_row in summary_rows:
            _date = summary_row.date[0:10]
            if summary_row.cluster not in _summary_recap:
                _summary_recap[summary_row.cluster] = {}

            if _date not in _summary_recap[summary_row.cluster]:
                _summary_recap[summary_row.cluster][_date] = 0

            _summary_recap[summary_row.cluster][_date] += summary_row.moved_files

            if summary_row.cluster not in _cluster_to_place:
                _cluster_to_place[summary_row.cluster] = summary_row.place

        for cluster, dates in _summary_recap.items():
            _f = True
            for date, counter in dates.items():
                if _f:
                    sub_summary_rows.append(SubSummaryRow(cluster, _cluster_to_place[cluster], date, counter))
                else:
                    sub_summary_rows.append(SubSummaryRow('', '', date, counter))
                _f = False

        logging.info('\nSummary recap ------------------------------------------------------------------------------------')
        logging.info(
            tabulate.tabulate(
                sub_summary_rows,
                headers=SubSummaryRow._fields, tablefmt="pipe"))

    def check():
        # initial checks
        logging.debug("Checking pre-requisites...")
        if not shutil.which("exiftool"):
            raise ValueError("Exiftool is not found in this system")

        if args.rename_only and args.destination:
            raise ValueError("Cannot specify both --rename-only and --destination options")

    def initialize_context():
        loc_settings = LocationSettings(
            'none' if args.skip_location else 'full',
            args.search_radius,
            args.cache if args.cache else tempfile.gettempdir()
        )

        logging_settings = LoggingSettings(
            args.verbose,
            args.summary,
            args.debug
        )

        file_settings = FileSettings(
            search_dir,
            args.recursive,
            dest_dir,
            args.dry_run,
            args.rename_only,
            args.rename_format
        )

        _ctx = Context(
            loc_settings,
            logging_settings,
            file_settings,
            gmaps,
            api_key
        )

        Context.set(_ctx)

        return _ctx

    logging.basicConfig(
        format='%(message)s',
        level=logging.DEBUG if args.verbose else logging.INFO)
    logging.getLogger("geopy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # performs checks
    check()

    # Prepare settings
    search_dir = os.path.abspath(os.path.expanduser(args.search_dir)) if args.search_dir else os.getcwd()
    assert os.path.isdir(search_dir) and os.path.exists(search_dir), "Search directory is invalid or it doesn't exist"
    dest_dir = os.path.abspath(os.path.expanduser(args.destination)) if args.destination else search_dir

    logging.debug("Initializing Phototripper...")
    api_key = get_google_api_key()
    gmaps: googlemaps.Client = googlemaps.Client(api_key)

    all_pictures: list[PictureInfo] = []
    geo_clusters: list[PictureCluster] = []
    locations: list[PictureLocation] = []
    summary_rows: list[SummaryRow] = []

    context: Context = initialize_context()

    if args.summary:
        logging.info("============================================================================")
        logging.info(context.to_summary())

    start_time = time.perf_counter()

    collect()
    move()

    if args.summary:
        print_summary()

    logging.info(f"Done in {time.perf_counter() - start_time:.3f} seconds")
