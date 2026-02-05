import configparser
import os
from collections import namedtuple
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS = 6371000

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
