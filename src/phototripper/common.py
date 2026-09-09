import configparser
import logging
import os
from collections import namedtuple
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS = 6371000

# ---------------------------------------------------------------------------
# Application config file support
# ---------------------------------------------------------------------------

APP_CONFIG_FILENAME = "phototripper.ini"

# ---------------------------------------------------------------------------
# Defaults registry
#
# Settings live in two scopes: the ``[common]`` ones, shared by every
# subcommand, and the module specific ones each subcommand registers for
# itself at import time via ``register_defaults``.
# ---------------------------------------------------------------------------

COMMON_DEFAULTS = {
    "search-dir": None,
    "filter": None,
    "recursive": False,
    "verbose": False,
    "summary": False,
    "dry-run": False,
}

_MODULE_DEFAULTS: dict[str, dict] = {}


def _to_bool(value):
    return value.lower() in ("true", "yes", "1", "on")


# INI values arrive as strings; a key listed here is converted on the way in.
# Keys are unique across the sections, so one flat table serves them all.
_CONVERTERS = dict.fromkeys(("recursive", "verbose", "summary", "dry-run"),
                            _to_bool)


def register_defaults(module_name, defaults, bools=(), ints=()):
    """Declare the settings owned by *module_name*.

    Called at import time by each subcommand module. *defaults* maps INI-style
    keys (``"max-int-secs"``) to their hardcoded default; *bools* and *ints*
    list the keys needing conversion from the INI string values.
    """
    _MODULE_DEFAULTS[module_name] = dict(defaults)
    _CONVERTERS.update(dict.fromkeys(bools, _to_bool))
    _CONVERTERS.update(dict.fromkeys(ints, int))


def registered_modules():
    """Return the names of the modules that declared their own settings."""
    return sorted(_MODULE_DEFAULTS)


def _find_config_file(filename=APP_CONFIG_FILENAME):
    """Search standard paths and return the first existing config file, or None."""
    def _env(var):
        return os.environ.get(var, "")

    candidates = [
        os.path.join(os.getcwd(), filename),
        os.path.join(_env("HOME"), filename),
        os.path.join(_env("HOME"), ".config", filename),
        os.path.join(_env("PHOTOTRIPPER_CONFIG"), filename),
    ]

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def _read_app_config():
    """Parse the first found phototripper.ini and return the ConfigParser."""
    path = _find_config_file()
    if path is None:
        return None
    parser = configparser.ConfigParser()
    parser.read(path)
    return parser


def load_app_config(module_name=None):
    """Return merged config: hardcoded < [common] < [module].

    *module_name* is e.g. ``"sort"`` or ``"gpx"``; ``None`` restricts the
    result to the shared ``[common]`` settings.
    Returns a dict keyed by INI-style names (``"search-dir"``).
    """
    merged = COMMON_DEFAULTS | _MODULE_DEFAULTS.get(module_name, {})

    cfg = _read_app_config()
    if cfg is None:
        return merged

    # overlay [common] -- a shared section may still set a module owned key
    if cfg.has_section("common"):
        for key, value in cfg.items("common"):
            if key in merged:
                merged[key] = _CONVERTERS.get(key, str)(value)

    # overlay [module]
    if module_name and cfg.has_section(module_name):
        for key, value in cfg.items(module_name):
            if key in merged:
                merged[key] = _CONVERTERS.get(key, str)(value)

    return merged


def apply_config_to_args(args, module_name):
    """Fill ``None`` CLI args with values from the config file.

    Only the keys visible to *module_name* are touched.
    If the argparse attribute is *not* ``None`` the CLI value wins.
    """
    if module_name is None:
        return

    config = load_app_config(module_name)

    for ini_key, value in config.items():
        attr = ini_key.replace("-", "_")
        if not hasattr(args, attr):
            continue
        if getattr(args, attr) is None:
            setattr(args, attr, value)


def get_google_api_key():
    """Return the Google API key.

    Lookup order:
      1. ``[google] api-key`` in phototripper.ini
      2. Legacy ``.pyscripts-google.ini`` file (backward compat)
    """
    cfg = _read_app_config()
    if cfg and cfg.has_section("google") and cfg.has_option("google", "api-key"):
        value = cfg.get("google", "api-key").strip()
        if value and value != "YOUR_KEY_HERE":
            return value

    # Legacy fallback
    legacy = load_configuration(".pyscripts-google.ini")
    return legacy["google"]["api-key"]

LocationSettings = namedtuple("LocationSettings", "strategy, search_radius, cache_dir")
LoggingSettings = namedtuple("LoggingSettings", "verbose, print_summary, debug")
FileSettings = namedtuple("FileSettings", "search_dir, recursive_search, dest_dir, dry_run, rename_only, rename_pattern")
GpxSettings = namedtuple("GpxSettings",
                         "track_files, max_int_secs, max_ext_secs, geosync, "
                         "sidecar, overwrite_gps, skip_dst_check, logger")


def _yes_no(flag):
    return 'yes' if flag else 'no'


class Context:
    context: 'Context' = None

    def __init__(self, 
                 location_settings: LocationSettings,
                 logging_settings: LoggingSettings,
                 file_settings: FileSettings,
                 gpx_settings: GpxSettings | None = None):

        self.location_settings: LocationSettings = location_settings
        self.logging_settings: LoggingSettings = logging_settings
        self.file_settings: FileSettings = file_settings
        self.gpx_settings: GpxSettings | None = gpx_settings
        self._api_key = None
        self._gmaps = None

    # The key and the client are resolved on first use: a run that never asks a
    # location service anything must not require a configured API key.
    @property
    def api_key(self):
        if self._api_key is None:
            self._api_key = get_google_api_key()
        return self._api_key

    @property
    def gmaps(self):
        if self._gmaps is None:
            import googlemaps

            self._gmaps = googlemaps.Client(self.api_key)
        return self._gmaps

    def to_summary(self):
        """Render only the settings that apply to the running subcommand."""
        return "\n".join(block for block in (self._gpx_block(),
                                             self._file_block(),
                                             self._location_block(),
                                             self._logging_block()) if block)

    def _gpx_block(self):
        if self.gpx_settings is None:
            return ""

        gpx = self.gpx_settings
        return f"""GPX settings:
    Track files             : {", ".join(gpx.track_files)}
    Max interpolation (s)   : {gpx.max_int_secs}
    Max extrapolation (s)   : {gpx.max_ext_secs}
    Camera clock sync       : {gpx.geosync or 'none'}
    Write to                : {'XMP sidecar' if gpx.sidecar else 'original file'}
    Overwrite existing GPS  : {_yes_no(gpx.overwrite_gps)}
    Check camera DST        : {_yes_no(not gpx.skip_dst_check)}
    GPS logger              : {gpx.logger or 'none'}
"""

    def _file_block(self):
        files = self.file_settings

        # only the subcommands that move files have somewhere to move them to
        moving = f"""    Destination directory   : {files.dest_dir}
    Rename only             : {_yes_no(files.rename_only)}
    Rename pattern          : {files.rename_pattern}
""" if files.rename_pattern else ""

        return f"""File settings:
    Search directory        : {files.search_dir}
    Recursive search        : {_yes_no(files.recursive_search)}
    Dry run                 : {_yes_no(files.dry_run)}
""" + moving

    def _location_block(self):
        if self.location_settings is None:
            return ""

        location = self.location_settings
        return f"""Location settings:
    Strategy                : {location.strategy}
    Search radius (m)       : {location.search_radius}
    Cache directory         : {location.cache_dir}
"""

    def _logging_block(self):
        return f"""Logging settings:
    Verbose                 : {_yes_no(self.logging_settings.verbose)}
    Print summary           : {_yes_no(self.logging_settings.print_summary)}
    Debug                   : {_yes_no(self.logging_settings.debug)}
"""

    @staticmethod
    def set(ctx: 'Context'):
        if Context.context is not None:
            raise RuntimeError("Context has already been initalized")
        Context.context = ctx

    @staticmethod
    def get() -> 'Context':
        if Context.context is None:
            raise RuntimeError("Context has not been initalized yet")
        return Context.context


def load_configuration(configuration_file, parser=None):
    def _x(var_name):
        return os.environ.get(var_name, 'THIS_FOLDER_DOESNT_EXIST')

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
        raise ValueError(
            "Cannot find configuration file %s. "
            "Put file in $HOME, $HOME/.config/ or specify the "
            "configuration folder using the environment "
            "variable $PYSCRIPTS_CONFIG" % configuration_file
        )


# ---------------------------------------------------------------------------
# Shared logging and file discovery
# ---------------------------------------------------------------------------

SUPPORTED_RAW_EXT = ["arw"]


def setup_logging(verbose):
    """Configure the root logger the way every subcommand expects it."""
    logging.basicConfig(
        format='%(message)s',
        level=logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("geopy").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def expand_path(path):
    """Make *path* absolute, expanding ``~``; an empty path means the cwd."""
    return os.path.abspath(os.path.expanduser(path)) if path else os.getcwd()


def resolve_target(path):
    """Return ``(root, single_file)`` for the requested search location.

    *path* may be a directory or a single file, absolute or relative, and may
    start with ``~``; an empty *path* means the current directory. ``root`` is
    always a directory -- the containing one when *path* names a file. Unlike
    :func:`expand_path` the location must already exist.
    """
    target = expand_path(path)

    if os.path.isdir(target):
        return target, None
    if os.path.isfile(target):
        return os.path.dirname(target), target

    raise ValueError(f"Search directory is invalid or it doesn't exist: {target}")


def discover_files(path, recursive=False, name_filter=None, extensions=SUPPORTED_RAW_EXT):
    """Return the media files to work on, in a stable order.

    *path* is a directory to scan or a single file to work on. Only files
    whose extension is listed in *extensions* are returned, and *name_filter*
    further keeps only the paths containing it as a substring.
    """
    def keep(file_path):
        _, ext = os.path.splitext(file_path)
        if ext.lstrip('.').lower() not in extensions:
            return False
        return not name_filter or name_filter in file_path

    root, single_file = resolve_target(path)

    if single_file:
        return [single_file] if keep(single_file) else []

    files = []
    for _root, _dirs, _files in os.walk(root, topdown=True):
        logging.info(f"Scanning {_root} ({len(_files)} files)...")
        _dirs.sort()
        _files.sort()

        for _file in [os.path.join(_root, f) for f in _files if keep(os.path.join(_root, f))]:
            logging.debug(f" > {_file[len(_root):]}")
            files.append(_file)

        if not recursive:
            break

    return files


def haversine(latlon1, latlon2):
    d_lat = radians(latlon2[0] - latlon1[0])
    d_lon = radians(latlon2[1] - latlon1[1])

    a = sin(d_lat / 2) ** 2 + cos(radians(latlon1[0])) * cos(radians(latlon2[0])) * sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS * asin(sqrt(a))
