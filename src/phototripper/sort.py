import argparse
import logging
import os
import re
import shutil
import tempfile
import time
import configparser
from collections import namedtuple
from datetime import datetime
from math import radians, cos, sin

import googlemaps
import tabulate
from unidecode import unidecode

from .common import Context, LocationSettings, LoggingSettings, FileSettings
from .location import GeoTraits, PictureLocation
from .picture import PictureInfo, PictureCluster

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

def _load_configuration(configuration_file, parser=None):
    def _x(var_name):
        return os.environ[var_name] if var_name in os.environ else 'THIS_FOLDER_DOESNT_EXIST'

    lookups = [
        os.path.expanduser(os.path.expandvars(configuration_file)),
        os.path.join(_x('HOME'), configuration_file),
        os.path.join(_x('HOME'), '.config', configuration_file),
        os.path.join(_x('PYSCRIPTS_CONFIG'), configuration_file),
    ]

    path = None

    for _path in lookups:
        if os.path.exists(_path):
            path = _path
            break

    if path:
        if parser is None:
            parser = configparser.ConfigParser()
        parser.read(path)
        return parser
    else:
        raise ValueError("Cannot find configuration file %s. Put file in $HOME, $HOME/.config/ or specify the "
                         "configuration folder using the environment variable $PYSCRIPTS_CONFIG" %
                         configuration_file)

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
        
class Sorter:
    def __init__(self, args):
        self.args = args
        self.all_pictures: list[PictureInfo] = []
        self.geo_clusters: list[PictureCluster] = []
        self.locations: list[PictureLocation] = []
        self.summary_rows: list[SummaryRow] = []
        self.context: Context = None
        self.api_key = None
        self.gmaps = None
        self.search_dir = None
        self.dest_dir = None
        
    def check(self):
        logging.debug("Checking pre-requisites...")
        if not shutil.which("exiftool"):
            raise ValueError("Exiftool is not found in this system")

        if self.args.rename_only and self.args.destination:
            raise ValueError("Cannot specify both --rename-only and --destination options")

    def initialize_context(self):
        loc_settings = LocationSettings(
            'none' if self.args.skip_location else 'full',
            self.args.search_radius,
            self.args.cache if self.args.cache else tempfile.gettempdir()
        )

        logging_settings = LoggingSettings(
            self.args.verbose,
            self.args.summary,
            self.args.debug
        )

        file_settings = FileSettings(
            self.search_dir,
            self.args.recursive,
            self.dest_dir,
            self.args.dry_run,
            self.args.rename_only,
            self.args.rename_format
        )

        _ctx = Context(
            loc_settings,
            logging_settings,
            file_settings,
            self.gmaps,
            self.api_key
        )

        Context.set(_ctx)
        self.context = _ctx
        return _ctx
        
    def collect(self):
        def add_picture_file(picture_file):
            logging.debug(f"Adding picture file {os.path.basename(picture_file)}")
            picture = PictureInfo(picture_file)

            self.all_pictures.append(picture)

            if picture.has_latlon():
                cluster_found = False
                for cluster in self.geo_clusters:
                    if cluster.is_in_range(picture):
                        cluster.add_picture(picture)
                        logging.debug(f" > picture falls in cluster {cluster.name} "
                                      f"with center {cluster.center} "
                                      f"together with {len(cluster.pictures)} pictures")
                        cluster_found = True
                        break

                if not cluster_found:
                    cluster = PictureCluster(f"cluster{len(self.geo_clusters) + 1}", picture)
                    self.geo_clusters.append(cluster)
                    logging.debug(f" > created new cluster {cluster.name} with center {cluster.center}")

        logging.info("Collecting pictures...")
        for _root, _dirs, _files in os.walk(self.search_dir, topdown=True):
            logging.info(f"Scanning {_root} ({len(_files)} files)...")
            _dirs.sort()
            _files.sort()
            for _pic_file in [os.path.join(_root, f) for f in _files if f[-3:].lower() in SUPPORTED_RAW_EXT]:
                if not self.args.filter or self.args.filter in _pic_file:
                    logging.debug(f" > {_pic_file[len(_root):]}")
                    add_picture_file(_pic_file)

            if not self.args.recursive:
                break

    def move(self):
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

        logging.info(f"Loading pictures metadata with location strategy {self.context.location_settings.strategy}...")
        for info in self.all_pictures:
            _pict_counter += 1
            _checkpoint_counter += 1
            if _checkpoint_counter > 50 and time.perf_counter() - _checkpoint_start > 10:
                _checkpoint_start = time.perf_counter()
                _checkpoint_counter = 0
                logging.info(f"Processing pictures ({_pict_counter}/{len(self.all_pictures)})...")

            if info.cluster is None or self.context.location_settings.strategy == 'none':
                info.get_place_name = place_none
            else:
                _location = next((_l for _l in self.locations if _l.cluster == info.cluster), None)
                if _location is None:
                    _location = PictureLocation(info.cluster)
                    self.locations.append(_location)
                info.get_place_name = place_full_strategy

            destination = os.path.join(self.dest_dir, _get_new_name(self.context.file_settings.rename_pattern, info))
            destination_folder, destination_basename = os.path.split(destination)

            if not self.args.dry_run:
                os.makedirs(destination_folder, exist_ok=True)

            moved_files = info.move_files(destination_folder, destination_basename)

            self.summary_rows.append(
                SummaryRow(
                    info.file[len(self.search_dir):],
                    destination[len(self.dest_dir):],
                    info.get_date_time().strftime("%Y-%m-%d %H:%M:%S"),
                    info.cluster.name if info.cluster else None,
                    info.get_place_name(),
                    len(moved_files)))

    def print_summary(self):
        logging.info(
            '\nSummary ------------------------------------------------------------------------------------------')
        logging.info(
            tabulate.tabulate(
                self.summary_rows,
                headers=SummaryRow._fields,
                tablefmt='pipe'))

        SubSummaryRow = namedtuple('SubSummaryRow', 'cluster_name, place, date, moved_files')
        sub_summary_rows = []

        _summary_recap = {}
        _cluster_to_place = {}

        for summary_row in self.summary_rows:
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
                
    def run(self):
        logging.basicConfig(
            format='%(message)s',
            level=logging.DEBUG if self.args.verbose else logging.INFO)
        logging.getLogger("geopy").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        self.check()

        self.search_dir = os.path.abspath(os.path.expanduser(self.args.search_dir)) if self.args.search_dir else os.getcwd()
        assert os.path.isdir(self.search_dir) and os.path.exists(self.search_dir), "Search directory is invalid or it doesn't exist"
        self.dest_dir = os.path.abspath(os.path.expanduser(self.args.destination)) if self.args.destination else self.search_dir

        logging.debug("Initializing Phototripper...")
        self.api_key = _load_configuration('.pyscripts-google.ini')['google']['api-key']
        self.gmaps = googlemaps.Client(self.api_key)

        self.initialize_context()

        if self.args.summary:
            logging.info("============================================================================")
            logging.info(self.context.to_summary())

        start_time = time.perf_counter()

        self.collect()
        self.move()

        if self.args.summary:
            self.print_summary()

        logging.info(f"Done in {time.perf_counter() - start_time:.3f} seconds")

def configure_parser(subparsers):
    parser = subparsers.add_parser('sort', help='Sort pictures based on location and date')
    
    file_group = parser.add_argument_group('File options')
    file_group.add_argument('-s', "--search-dir", help="Search directory")
    file_group.add_argument('-d', "--destination", help="Destination directory")
    file_group.add_argument('--filter', help="Filter files by substring match")
    file_group.add_argument("--recursive", action='store_true', help="Scan recursively files from root directory")
    file_group.add_argument("--rename-only", action='store_true', help="Only renames files (without moving them)")
    file_group.add_argument('-f', '--rename-format', default='{day} - {place}/IMG_{datetime:extended}_{sequence}',
                            help=f"""
Renames files according to the specified format, using substitution variables with syntax '{{<var>[:<format>]}}'. 
A slash will create a folder.  
Any dash, space or underscore before the variable will be stripped away if the variable can't be retrieved. 
The following variables are supported (star=default): {", ".join([_vvv_fff(_v) for _v in VARS.keys()])}.
Eg: '{{year}}/{{month}} - {{month:name}}/{{day}} - {{place}}/IMG_{{datetime}}_{{sequence}}' --> 
'2023/03 - March/15 - New York/IMG_20230315T143000_05'.
Default is '{{day}} - {{place}}/IMG_{{datetime:extended}}_{{sequence}}'""")

    log_group = parser.add_argument_group('Logging options')
    log_group.add_argument("--verbose", action='store_true', help="Logs more")
    log_group.add_argument('--summary', action='store_true', help="Show summary")
    log_group.add_argument("--dry-run", action='store_true', help="Don't move/rename files")

    loc_group = parser.add_argument_group('Location service')
    loc_group.add_argument("--skip-location", action='store_true', help="Don't use location services")
    loc_group.add_argument("--search-radius", help="Search radius", type=int, default=3000)
    loc_group.add_argument("--cache", help="JSON service cache folder (default is temp folder)")
    loc_group.add_argument("--debug", action='store_true', help="Debug geocoding decisions")
    
    parser.set_defaults(func=run)

def run(args):
    sorter = Sorter(args)
    sorter.run()
