import configparser
import os
from collections import namedtuple
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS = 6371000

# ---------------------------------------------------------------------------
# Application config file support
# ---------------------------------------------------------------------------

APP_CONFIG_FILENAME = "phototripper.ini"

HARDCODED_DEFAULTS = {
    "search-dir": None,
    "destination": None,
    "filter": None,
    "recursive": False,
    "rename-only": False,
    "rename-format": "{day} - {place}/IMG_{datetime:extended}_{sequence}",
    "verbose": False,
    "summary": False,
    "dry-run": False,
    "skip-location": False,
    "search-radius": 3000,
    "cache": None,
    "debug": False,
}

# Keys that need type conversion from INI string values
_BOOL_KEYS = {
    "recursive", "rename-only", "verbose", "summary",
    "dry-run", "skip-location", "debug",
}
_INT_KEYS = {"search-radius"}

# INI key  ->  argparse dest attribute name
_KEY_MAP = {k: k.replace("-", "_") for k in HARDCODED_DEFAULTS}


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


def _convert(key, value):
    """Convert an INI string value to the appropriate Python type."""
    if key in _BOOL_KEYS:
        return value.lower() in ("true", "yes", "1", "on")
    if key in _INT_KEYS:
        return int(value)
    return value


def load_app_config(module_name=None):
    """Return merged config: hardcoded < [common] < [module].

    *module_name* is e.g. ``"sort"`` or ``"gpx"``.
    Returns a dict keyed by INI-style names (``"search-dir"``).
    """
    merged = dict(HARDCODED_DEFAULTS)

    cfg = _read_app_config()
    if cfg is None:
        return merged

    # overlay [common]
    if cfg.has_section("common"):
        for key, value in cfg.items("common"):
            if key in merged:
                merged[key] = _convert(key, value)

    # overlay [module]
    if module_name and cfg.has_section(module_name):
        for key, value in cfg.items(module_name):
            if key in merged:
                merged[key] = _convert(key, value)

    return merged


def apply_config_to_args(args, module_name):
    """Fill ``None`` CLI args with values from the config file.

    Only keys present in ``HARDCODED_DEFAULTS`` are touched.
    If the argparse attribute is *not* ``None`` the CLI value wins.
    """
    if module_name is None:
        return

    config = load_app_config(module_name)

    for ini_key, attr in _KEY_MAP.items():
        if not hasattr(args, attr):
            continue
        if getattr(args, attr) is None:
            setattr(args, attr, config[ini_key])


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


class Context:
    context: 'Context' = None

    def __init__(self, 
                 location_settings: LocationSettings,
                 logging_settings: LoggingSettings,
                 file_settings: FileSettings,
                 gmaps, api_key):
        
        self.location_settings: LocationSettings = location_settings
        self.logging_settings: LoggingSettings = logging_settings
        self.file_settings: FileSettings = file_settings
        self.gmaps = gmaps
        self.api_key = api_key

    def to_summary(self):
        return f"""File settings:
    Search directory        : {self.file_settings.search_dir}
    Recursive search        : {'yes' if self.file_settings.recursive_search else 'no'}
    Destination directory   : {self.file_settings.dest_dir}
    Dry run                 : {'yes' if self.file_settings.dry_run else 'no'}
    Rename only             : {'yes' if self.file_settings.rename_only else 'no'}
    Rename pattern          : {self.file_settings.rename_pattern}

Location settings:
    Strategy                : {self.location_settings.strategy}
    Search radius (m)       : {self.location_settings.search_radius}
    Cache directory         : {self.location_settings.cache_dir}

Logging settings:
    Verbose                 : {'yes' if self.logging_settings.verbose else 'no'}
    Print summary           : {'yes' if self.logging_settings.print_summary else 'no'}
    Debug                   : {'yes' if self.logging_settings.debug else 'no'}
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


def haversine(latlon1, latlon2):
    d_lat = radians(latlon2[0] - latlon1[0])
    d_lon = radians(latlon2[1] - latlon1[1])

    a = sin(d_lat / 2) ** 2 + cos(radians(latlon1[0])) * cos(radians(latlon2[0])) * sin(d_lon / 2) ** 2
    return 2 * EARTH_RADIUS * asin(sqrt(a))
