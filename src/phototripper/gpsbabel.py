"""Everything that knows about gpsbabel.

The GPS loggers phototripper talks to are read through
`gpsbabel <https://www.gpsbabel.org/>`_, which needs three things nobody knows
for a new device: an input format, a serial port, and a set of format options.
This module holds the knowledge of how to ask gpsbabel what it can do, how to
probe a device until something answers, and how to record the answer as a named
profile in ``phototripper.ini``.

Nothing here writes to a logger on its own: the callers decide, and every
destructive option is spelled out in the profile rather than assumed.
"""

import configparser
import glob
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections import namedtuple
from datetime import datetime
from functools import cache
from itertools import count

from .common import APP_CONFIG_FILENAME, _find_config_file, _read_app_config
from .track import GpsTrack

# A serial port, as the system describes it. *device* is the name worth
# recording -- the /dev/serial/by-id one where there is one -- while *tty* is
# the short /dev/ttyACM0 the same port also answers to.
Port = namedtuple("Port", "device, tty, usb_id, manufacturer, product")

# A gpsbabel format and the options it accepts, keyed by option name.
Format = namedtuple("Format", "name, description, options")

# A named logger, as stored in a [logger:<name>] section.
Profile = namedtuple("Profile", "name, format, device, options, description",
                     defaults=("", ""))

# What one gpsbabel invocation did.
Result = namedtuple("Result", "argv, returncode, stdout, stderr, timed_out")

# What one probe attempt found. *error* is None when gpsbabel came back
# cleanly, which is the real sign that the format was right: whether the
# logger had anything recorded is a separate question, answered by *points*.
Attempt = namedtuple("Attempt", "format, options, points, error")

# Seconds a single probe attempt may take. Not a user setting: a wrong guess
# errors out on its own long before this, and a whole download off a logger
# that does answer takes well under a second.
PROBE_TIMEOUT = 20

# Sections holding a logger are named after the device, not after a subcommand.
SECTION_PREFIX = "logger:"

# Where the system describes its serial ports. Module level so tests can point
# them somewhere harmless.
BY_ID_DIR = "/dev/serial/by-id"
TTY_GLOBS = ("/dev/ttyUSB*", "/dev/ttyACM*")
SYS_TTY_DIR = "/sys/class/tty"

# Options that erase a logger. A probe must never pass one of these.
DESTRUCTIVE_OPTIONS = frozenset({"erase", "erase_only", "no-output"})

# ---------------------------------------------------------------------------
# Hints: the baud rates worth trying first, per format. Ordering only -- a
# format missing from here is still probed, just later and at whatever baud
# rate gpsbabel defaults to.
#
# There is deliberately no "read less to probe faster" shortcut. skytraq's
# last-sector=0 looked like one, but reading a single sector returns no track
# points on a logger that has plenty, which is indistinguishable from the
# wrong format; and a whole download turns out to take a third of a second,
# so it bought nothing either.
# ---------------------------------------------------------------------------

FORMAT_HINTS = {
    "skytraq": (230400, 115200, 57600, 9600),
    "miniHomer": (115200, 38400),
    "mtk": (115200, 38400),
    "m241": (38400,),
    "dg-100": (),
    "dg-200": (),
}


def check():
    """Fail early, and in the user's language, when gpsbabel is missing."""
    if not shutil.which("gpsbabel"):
        raise ValueError(
            "gpsbabel is not found in this system: it is what reads the GPS "
            "logger. Install it from https://www.gpsbabel.org/")


# ---------------------------------------------------------------------------
# What this gpsbabel build can do
# ---------------------------------------------------------------------------

def parse_formats(text):
    """Read the ``gpsbabel -^3`` dump into ``{name: Format}``.

    The dump is tab separated and has two kinds of row::

        serial\\tr-r---\\tskytraq\\t\\tSkyTraq Venus based loggers\\tskytraq
        option\\tskytraq\\terase\\tErase device data after download\\tboolean...

    Only serial formats that can *read tracks* are kept -- the read/write flags
    are six characters, waypoints, tracks and routes in pairs, so track reading
    is the third one.
    """
    formats = {}
    options = {}

    for line in text.splitlines():
        fields = line.split("\t")

        if fields[0] == "serial" and len(fields) >= 5:
            flags, name, description = fields[1], fields[2], fields[4]
            if len(flags) >= 3 and flags[2] == "r":
                formats[name] = Format(name, description, {})

        elif fields[0] == "option" and len(fields) >= 5:
            options.setdefault(fields[1], {})[fields[2]] = fields[4]

    return {name: fmt._replace(options=options.get(name, {}))
            for name, fmt in formats.items()}


@cache
def list_serial_formats():
    """Ask gpsbabel which serial formats can download a track.

    ``-^3`` is the machine readable dump the gpsbabel GUI uses. It is not in
    the shipped documentation, and it is also the only interface that says
    whether a format is serial rather than a file, which is exactly what has to
    be known here.
    """
    result = run(["gpsbabel", "-^3"], timeout=PROBE_TIMEOUT)

    formats = parse_formats(result.stdout) if result.returncode == 0 else {}
    if not formats:
        raise ValueError(
            "This gpsbabel would not say which serial formats it has, so "
            "there is no way to know what to try. Check that 'gpsbabel -^3' "
            "prints a list.")

    logging.debug(f" > gpsbabel offers {len(formats)} serial format(s) reading tracks")
    return formats


def erase_options_for(fmt_name):
    """The two erase spellings *fmt_name* accepts, as ``(after download, only)``.

    Erasing after a download is ``erase`` everywhere, but erasing on its own is
    not: skytraq and miniHomer want ``erase,no-output`` while the MTK based
    loggers have a dedicated ``erase_only``. Reading it off the format's own
    option list means no device needs a special case here, and no profile has
    to record a spelling gpsbabel can be asked for.
    """
    options = list_serial_formats()[fmt_name].options

    if "erase" not in options:
        return "", ""

    if "no-output" in options:
        return "erase", "erase,no-output"
    if "erase_only" in options:
        return "erase", "erase_only"

    # it can be cleared as part of a download, but not on its own
    return "erase", ""


# ---------------------------------------------------------------------------
# What is plugged in
# ---------------------------------------------------------------------------

def _usb_attributes(device):
    """Vendor and product of the USB device behind a tty, from sysfs.

    ``/sys/class/tty/<name>/device`` is a symlink into the device tree,
    pointing at the USB *interface*; the vendor and product live a level or two
    above it. So the link is resolved first -- walking up an unresolved symlink
    only ever climbs back out of /sys/class/tty -- and then followed until a
    directory carries ``idVendor``.

    Costs no dependency, which matters: pyserial is not installed and this is
    not worth adding one for.
    """
    def read(path):
        try:
            with open(path) as handle:
                return handle.read().strip()
        except OSError:
            return None

    tty = os.path.basename(os.path.realpath(device))
    path = os.path.realpath(os.path.join(SYS_TTY_DIR, tty, "device"))

    for _ in range(6):
        if not os.path.isdir(path):
            break
        if os.path.exists(os.path.join(path, "idVendor")):
            vendor = read(os.path.join(path, "idVendor"))
            product = read(os.path.join(path, "idProduct"))
            usb_id = f"{vendor}:{product}" if vendor and product else None
            return (usb_id,
                    read(os.path.join(path, "manufacturer")),
                    read(os.path.join(path, "product")))
        path = os.path.dirname(path)

    return None, None, None


def list_serial_ports():
    """Every serial port on the system, described as well as sysfs allows.

    ``/dev/serial/by-id`` is preferred because its names survive a reboot and a
    different USB socket, which is what makes a saved profile keep working.
    """
    devices = {}

    if os.path.isdir(BY_ID_DIR):
        for entry in sorted(os.listdir(BY_ID_DIR)):
            link = os.path.join(BY_ID_DIR, entry)
            devices[os.path.realpath(link)] = link

    for pattern in TTY_GLOBS:
        for device in sorted(glob.glob(pattern)):
            devices.setdefault(device, device)

    ports = []
    for real, name in sorted(devices.items()):
        usb_id, manufacturer, product = _usb_attributes(real)
        ports.append(Port(name, real, usb_id, manufacturer, product))

    return ports


# ---------------------------------------------------------------------------
# Saved profiles
# ---------------------------------------------------------------------------

def list_profiles():
    """Every ``[logger:<name>]`` section in the config file."""
    cfg = _read_app_config()
    if cfg is None:
        return {}

    profiles = {}
    for section in cfg.sections():
        if not section.startswith(SECTION_PREFIX):
            continue

        name = section[len(SECTION_PREFIX):].strip()
        values = dict(cfg.items(section))

        missing = [key for key in ("format", "device") if not values.get(key)]
        if missing:
            logging.warning(
                f"Ignoring [{section}]: it has no {' and no '.join(missing)}.")
            continue

        profiles[name] = Profile(
            name,
            values["format"],
            values["device"],
            values.get("options", ""),
            values.get("description", ""),
        )

    return profiles


def load_profile(name):
    """The profile called *name*, or a message naming the ones that exist."""
    profiles = list_profiles()

    if name in profiles:
        return profiles[name]

    if not profiles:
        raise ValueError(
            f"No GPS logger called '{name}' is configured, and no logger is "
            f"configured at all. Run 'phototripper gpslogger detect --save "
            f"{name}' to set one up.")

    known = ", ".join(sorted(profiles))
    raise ValueError(
        f"No GPS logger called '{name}' is configured. Known logger(s): {known}")


def resolve_profile(name=None):
    """The logger to talk to.

    Naming one is only necessary when there is a choice: a single saved logger
    is the obvious answer and asking for it again would be noise.
    """
    if name:
        return load_profile(name)

    profiles = list_profiles()

    if not profiles:
        raise ValueError(
            "No GPS logger is configured. Plug the logger in and run "
            "'phototripper gpslogger detect --save <name>' to find its "
            "settings and record them.")

    if len(profiles) > 1:
        known = ", ".join(sorted(profiles))
        raise ValueError(
            f"Several GPS loggers are configured ({known}): say which one "
            f"with --logger, or set 'logger' in the [gpx] section of "
            f"{APP_CONFIG_FILENAME}.")

    profile = next(iter(profiles.values()))
    logging.debug(f" > using the only configured logger: {profile.name}")
    return profile


def _profile_block(profile):
    """The INI text of a profile, as a person would have typed it."""
    lines = [f"[{SECTION_PREFIX}{profile.name}]"]

    if profile.description:
        lines.append(f"description = {profile.description}")

    lines.append(f"format = {profile.format}")
    lines.append(f"device = {profile.device}")

    if profile.options:
        lines.append(f"options = {profile.options}")

    return "\n".join(lines) + "\n"


def _replace_section(text, section, block):
    """Swap one INI section for *block*, leaving the rest of the file alone."""
    out = []
    skipping = False

    for line in text.splitlines(keepends=True):
        header = line.strip()

        if header.startswith("[") and header.endswith("]"):
            if skipping:
                skipping = False
            if header == f"[{section}]":
                out.append(block)
                skipping = True
                continue

        if not skipping:
            out.append(line)

    return "".join(out)


def save_profile(profile, force=False):
    """Record *profile* in the config file and return where it went.

    The section is appended as text rather than written through ConfigParser:
    the shipped template is almost entirely comments explaining what each
    setting does, and ``ConfigParser.write`` would throw all of them away.
    """
    path = _find_config_file()

    if path is None:
        from .config import _do_init  # only needed when there is no config yet
        _do_init()
        path = _find_config_file()

        if path is None:
            raise ValueError(
                f"Could not create a {APP_CONFIG_FILENAME} to save the logger "
                f"in. Run 'phototripper config --init' first.")

    section = f"{SECTION_PREFIX}{profile.name}"
    text = ""
    if os.path.exists(path):
        with open(path) as handle:
            text = handle.read()

    cfg = configparser.ConfigParser()
    cfg.read_string(text)

    if cfg.has_section(section):
        if not force:
            raise ValueError(
                f"A logger called '{profile.name}' is already configured in "
                f"{path}. Use --force to replace it.")
        updated = _replace_section(text, section, _profile_block(profile))
    else:
        separator = "" if text.endswith("\n\n") or not text else "\n"
        updated = text + separator + _profile_block(profile)

    with open(path, "w") as handle:
        handle.write(updated)

    return path


# ---------------------------------------------------------------------------
# Building and running the commands
# ---------------------------------------------------------------------------

def _input_spec(profile, extra=()):
    """The ``-i`` argument: the format and everything it should do."""
    parts = [profile.format]

    if profile.options:
        parts += [part.strip() for part in profile.options.split(",") if part.strip()]

    parts += [part for part in extra if part]

    return ",".join(parts)


def to_gpsbabel_time(text):
    """Turn a date the user typed into the ``YYYYMMDDHHMMSS`` gpsbabel wants.

    The track filter reads its bounds in UTC, so the help text says UTC and
    nothing here shifts anything.
    """
    try:
        return datetime.fromisoformat(text.strip()).strftime("%Y%m%d%H%M%S")
    except ValueError as error:
        raise ValueError(
            f"'{text}' is not a date I can read. Use YYYY-MM-DD, optionally "
            f"with a time: 2024-06-01T10:30 (UTC).") from error


def build_extract_argv(profile, out_file, wipe=False, start=None, stop=None):
    """The command that downloads the track, and optionally clears the logger.

    Clearing is folded into the download on purpose: gpsbabel erases only once
    it has written the file, so nothing is lost that was not saved first.
    """
    erase = erase_options_for(profile.format)[0] if wipe else ""

    if wipe and not erase:
        raise ValueError(
            f"The '{profile.format}' format has no option to erase the logger, "
            f"so --wipe cannot be honoured.")

    argv = ["gpsbabel", "-t",
            "-i", _input_spec(profile, [erase]),
            "-f", profile.device]

    window = [f"{key}={value}"
              for key, value in (("start", start), ("stop", stop)) if value]
    if window:
        argv += ["-x", "track," + ",".join(window)]

    argv += ["-o", "gpx", "-F", out_file]

    return argv


def build_wipe_argv(profile):
    """The command that clears the logger without downloading anything."""
    clean = erase_options_for(profile.format)[1]

    if not clean:
        raise ValueError(
            f"The '{profile.format}' format cannot be cleared on its own. Use "
            f"'gpslogger extract --wipe' to download the track and clear the "
            f"logger in one go.")

    # no -o/-F: 'no-output' and 'erase_only' both write nothing
    return ["gpsbabel", "-t",
            "-i", _input_spec(profile, [clean]),
            "-f", profile.device]


def build_probe_argv(device, fmt_name, options, out_file):
    """A read-only download, used to find out whether a device answers."""
    parts = [part for part in options if part]

    destructive = DESTRUCTIVE_OPTIONS.intersection(
        part.split("=")[0] for part in parts)
    if destructive:
        # a probe talks to hardware nobody has identified yet; it must never
        # be the thing that erases it
        raise ValueError(f"A probe must not pass {', '.join(sorted(destructive))}")

    return ["gpsbabel", "-t",
            "-i", ",".join([fmt_name, *parts]),
            "-f", device,
            "-o", "gpx", "-F", out_file]


def run(argv, timeout=None):
    """Run gpsbabel and hand back everything it said."""
    logging.debug(f" > {' '.join(argv)}")

    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(argv, None, "", "", True)

    return Result(argv, completed.returncode,
                  completed.stdout, completed.stderr, False)


def read_points(path):
    """How many timed points landed in *path*."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return 0

    try:
        return len(GpsTrack.read([path]))
    except ValueError:
        return 0


def probe(device, fmt_name, options=()):
    """Try one format against one port, reading nothing back onto the device.

    A clean return says the format was understood. An empty logger returns
    no points and is still a match, so the two are reported apart.
    """
    handle, out_file = tempfile.mkstemp(suffix=".gpx")
    os.close(handle)

    try:
        argv = build_probe_argv(device, fmt_name, options, out_file)
        result = run(argv, timeout=PROBE_TIMEOUT)

        if result.timed_out:
            return Attempt(fmt_name, options, 0, "no response")

        if result.returncode != 0:
            return Attempt(fmt_name, options, 0, _first_error(result.stderr))

        return Attempt(fmt_name, options, read_points(out_file), None)
    finally:
        if os.path.exists(out_file):
            os.unlink(out_file)


def _first_error(stderr):
    """The one line of gpsbabel's complaint worth showing in a table."""
    for line in (line.strip() for line in stderr.splitlines()):
        if line:
            return re.sub(r"\s+", " ", line)[:60]
    return "failed"


# ---------------------------------------------------------------------------
# Talking to the device, and to the user
# ---------------------------------------------------------------------------

def sanity_check(profile):
    """Check what can be checked before a single byte reaches the logger.

    gpsbabel has no dry run for a serial device, so everything that can be
    established from the file system and from gpsbabel's own capabilities is
    established here, while nothing has happened yet.
    """
    check()

    formats = list_serial_formats()

    if profile.format not in formats:
        known = ", ".join(sorted(formats))
        raise ValueError(
            f"This gpsbabel does not have a serial format called "
            f"'{profile.format}'. It offers: {known}")

    accepted = formats[profile.format].options
    unknown = [part.split("=")[0]
               for part in _input_spec(profile).split(",")[1:]
               if part.split("=")[0] not in accepted]
    if unknown:
        raise ValueError(
            f"The '{profile.format}' format does not accept "
            f"{', '.join(unknown)}. Fix 'options' in the "
            f"[{SECTION_PREFIX}{profile.name}] section.")

    device = profile.device

    if not os.path.exists(device):
        raise ValueError(
            f"The logger's port {device} is not there. Check that it is "
            f"plugged in and switched on, or run 'phototripper gpslogger "
            f"detect' to find where it is now.")

    real = os.path.realpath(device)

    try:
        mode = os.stat(real).st_mode
    except OSError as error:
        raise ValueError(f"Cannot read {device}: {error}") from error

    if not stat.S_ISCHR(mode):
        raise ValueError(
            f"{device} is not a serial port. A logger's port looks like "
            f"/dev/ttyUSB0, or a name under {BY_ID_DIR}.")

    if not os.access(real, os.R_OK | os.W_OK):
        raise ValueError(
            f"No permission to use {device}. On most systems reading a serial "
            f"port needs membership of the 'dialout' group: "
            f"sudo usermod -a -G dialout $USER, then log out and back in.")


def ask(question, assume_yes=False, refusal=""):
    """Put a yes/no question, and take silence for a no.

    Off a terminal there is nobody to ask, so the answer is no. Anything that
    reaches out and touches hardware goes through here.
    """
    if assume_yes:
        return True

    if not sys.stdin.isatty():
        logging.warning(f"{refusal} Pass --yes if that is really what you want.")
        return False

    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def sequential_name(path):
    """*path*, or the next free ``name-N`` beside it."""
    if not os.path.exists(path):
        return path

    root, ext = os.path.splitext(path)

    return next(name for index in count(1)
                if not os.path.exists(name := f"{root}-{index}{ext}"))
