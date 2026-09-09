"""The 'gpslogger' subcommand: everything that touches the GPS logger itself.

Finding out how to talk to a logger is a one-off job -- once ``detect`` has
written a ``[logger:<name>]`` section nobody thinks about baud rates again --
so it lives apart from ``gpx``, which is about pictures.

This module owns no settings. A probe timeout and a list of baud rates to try
are technical details of the hardware, not choices a photographer should have
to make, so they are constants in ``gpsbabel.py``.
"""

import logging
import os
from collections import namedtuple

import tabulate

from . import gpsbabel
from .common import setup_logging

# What the ports table shows. The short name is the one a person recognises;
# the stable one is too long to put in a column and is printed underneath.
PortRow = namedtuple("PortRow", "port, usb_id, product")

# What the profile table shows.
ProfileRow = namedtuple("ProfileRow",
                        "name, description, format, device, options")

DEFAULT_TRACK_NAME = "track.gpx"


def register_args(subparsers):
    parser = subparsers.add_parser(
        'gpslogger', help='Find, read and clear a GPS logger')

    verbs = parser.add_subparsers(dest='verb', required=True)

    detect = verbs.add_parser(
        'detect', help='Work out how to talk to a plugged in GPS logger')
    detect.add_argument('--save', metavar='NAME',
                        help="Record what worked as a named logger")
    detect.add_argument('--force', action='store_true', default=None,
                        help="Replace a logger of that name if there is one")
    # not a tuning knob: probing opens a serial port and sends a logger's init
    # bytes to whatever answers, so this is how you keep it off your other
    # hardware
    detect.add_argument('--device', help="Only probe this port")
    detect.add_argument("--yes", action='store_true', default=None,
                        help="Don't ask before opening the ports")
    detect.add_argument("--verbose", action='store_true', default=None,
                        help="Logs more")
    detect.set_defaults(func=run_detect)

    listing = verbs.add_parser('list', help='Show the configured GPS loggers')
    listing.add_argument("--verbose", action='store_true', default=None,
                         help="Logs more")
    listing.set_defaults(func=run_list)

    test = verbs.add_parser(
        'test', help='Check that a configured logger still answers')
    test.add_argument('name', nargs='?', help="Which logger; the only one by default")
    test.add_argument("--verbose", action='store_true', default=None,
                      help="Logs more")
    test.set_defaults(func=run_test)

    extract = verbs.add_parser(
        'extract', help='Download the track from the GPS logger')
    _add_logger_args(extract)
    extract.add_argument('-t', "--track", default=None,
                         help=f"Where to write the track "
                              f"(default {DEFAULT_TRACK_NAME} here; a name "
                              f"already taken becomes track-1.gpx)")
    extract.add_argument("--date-from", default=None,
                         help="Keep only points from this moment on, UTC "
                              "(YYYY-MM-DD, optionally THH:MM)")
    extract.add_argument("--date-to", default=None,
                         help="Keep only points up to this moment, UTC")
    extract.add_argument("--wipe", action='store_true', default=None,
                         help="Clear the logger once the track is written")
    extract.set_defaults(func=run_extract)

    wipe = verbs.add_parser(
        'wipe', help='Clear the GPS logger, downloading nothing first')
    _add_logger_args(wipe)
    wipe.set_defaults(func=run_wipe)


def _add_logger_args(parser):
    """The arguments every verb that talks to a configured logger shares."""
    parser.add_argument("--logger", default=None,
                        help="Which logger; needed only when several are set up")
    parser.add_argument("--yes", action='store_true', default=None,
                        help="Don't ask before clearing the logger")
    parser.add_argument("--verbose", action='store_true', default=None,
                        help="Logs more")
    parser.add_argument("--dry-run", action='store_true', default=None,
                        help="Show the gpsbabel command, run nothing")


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def _ordered_formats(formats):
    """The formats to try, likeliest first.

    FORMAT_HINTS names the loggers people actually own; everything else
    gpsbabel can download a track with is still tried, just later.
    """
    hinted = [name for name in gpsbabel.FORMAT_HINTS if name in formats]

    return hinted + sorted(set(formats) - set(hinted))


def _candidates(fmt_name, formats):
    """The option sets to try for one format, likeliest first."""
    bauds = gpsbabel.FORMAT_HINTS.get(fmt_name, ())

    if not bauds or "baud" not in formats[fmt_name].options:
        return [[]]

    return [[f"baud={baud}"] for baud in bauds]


def run_detect(args):
    setup_logging(args.verbose)
    gpsbabel.check()

    ports = _ports_to_probe(args.device)
    _report_ports(ports)

    # opening a serial port asserts DTR and RTS, which reboots Arduino and
    # ESP32 class boards -- before a byte of any protocol is sent
    logging.warning(
        "\nProbing opens each of these ports, which reboots Arduino and "
        "ESP32 class boards and interrupts whatever they were doing. Nothing "
        "is erased, and --device probes just one port.")

    if not gpsbabel.ask(f"Probe {len(ports)} port(s)?", args.yes,
                        "Nothing was probed:"):
        logging.info("Nothing was probed.")
        return

    formats = gpsbabel.list_serial_formats()

    logging.info("\nProbing (read-only -- nothing is erased)...")

    winner = _probe_ports(ports, formats)

    if winner is None:
        raise ValueError(_nothing_answered(ports))

    profile = _profile_from(winner, args.save or "logger")

    suggestion = gpsbabel.build_extract_argv(profile, DEFAULT_TRACK_NAME)
    logging.info(f"\nBest: {' '.join(suggestion)}")

    if not args.save:
        logging.info(
            "\nRun the same command with --save <name> to record this as a "
            "named logger.")
        return

    path = gpsbabel.save_profile(profile, force=bool(args.force))
    logging.info(f"Saved [{gpsbabel.SECTION_PREFIX}{args.save}] to {path}")


def _ports_to_probe(device):
    """The ports to try, honouring a user who named one."""
    if device:
        usb_id, manufacturer, product = gpsbabel._usb_attributes(device)
        return [gpsbabel.Port(device, os.path.realpath(device),
                              usb_id, manufacturer, product)]

    ports = gpsbabel.list_serial_ports()

    if not ports:
        raise ValueError(
            "No serial port found. Plug the GPS logger in, give it a moment, "
            "and try again; if it is on an unusual path, name it with --device.")

    return ports


def _nothing_answered(ports):
    """Say what was tried, so a logger that never appeared is not mistaken
    for one that refused.

    The two failures look the same in a table of unanswered probes and want
    opposite fixes: a logger that answered nothing needs different settings,
    while one the kernel never saw needs a switch, a cable, or a driver. Only
    the list of ports tells them apart, so it goes in the message.
    """
    tried = ", ".join(port.device for port in ports)

    return (
        f"No GPS logger answered on the {len(ports)} port(s) tried: {tried}\n"
        f"If your logger is not in that list, the computer never saw it: "
        f"switch it on, check the cable carries data and not only power, and "
        f"look for it in 'lsusb' or in 'sudo dmesg -w' as you plug it in.\n"
        f"If it is in that list, it was reached but spoke no format gpsbabel "
        f"knows -- and reading a port also needs membership of the 'dialout' "
        f"group on most systems."
    )


def _report_ports(ports):
    rows = [PortRow(port.tty, port.usb_id or '-',
                    " ".join(part for part in (port.manufacturer, port.product)
                             if part) or '-')
            for port in ports]

    logging.info("\nPorts ------------------------------------------------")
    logging.info(tabulate.tabulate(rows, headers=PortRow._fields, tablefmt='pipe'))

    # what gets saved is the by-id name, because /dev/ttyACM0 is handed out in
    # plug order and would point at the wrong device soon enough
    stable = [port for port in ports if port.device != port.tty]
    if stable:
        logging.info("\nRecorded by their stable name, which survives a reboot "
                     "and a different socket:")
        for port in stable:
            logging.info(f"  {port.tty} -> {port.device}")

    if len(ports) > 1:
        logging.info(
            "\nEvery one of these will be probed. Use --device to leave the "
            "rest of your hardware alone.")


def _probe_ports(ports, formats):
    """Try each port until something answers.

    A track coming back is the unambiguous hit and stops everything. A format
    that returns cleanly with nothing in it is kept as a fallback rather than
    thrown away: that is exactly what a logger with an empty memory looks
    like, and refusing it would mean a logger could only be set up while it
    happened to hold a trip.
    """
    fallback = None

    for port in ports:
        for fmt_name in _ordered_formats(formats):
            for options in _candidates(fmt_name, formats):
                attempt = gpsbabel.probe(port.device, fmt_name, options)

                logging.info(f"  {fmt_name:12s} {','.join(options) or '-':16s} "
                             f"... {_describe(attempt)}")

                if attempt.points:
                    return port, fmt_name, options

                if attempt.error is None and fallback is None:
                    fallback = (port, fmt_name, options)

    return fallback


def _describe(attempt):
    """The probe table's verdict on one attempt."""
    if attempt.points:
        return f"{attempt.points} points"
    if attempt.error is None:
        return "answered, nothing recorded"
    return attempt.error


def _profile_from(winner, name):
    """Turn a successful probe into something worth saving.

    The baud rate that answered is kept: it is the one thing about the probe
    a later download still needs. How the format erases is not, since
    gpsbabel can be asked that whenever it matters.
    """
    port, fmt_name, options = winner

    description = " ".join(part for part in (port.manufacturer, port.product)
                           if part)

    return gpsbabel.Profile(name, fmt_name, port.device, ",".join(options),
                            description)


# ---------------------------------------------------------------------------
# list and test
# ---------------------------------------------------------------------------

def run_list(args):
    setup_logging(args.verbose)

    profiles = gpsbabel.list_profiles()

    if not profiles:
        logging.info(
            "No GPS logger is configured. Plug one in and run "
            "'phototripper gpslogger detect --save <name>'.")
        return

    rows = [ProfileRow(profile.name, profile.description or '-', profile.format,
                       profile.device, profile.options or '-')
            for _, profile in sorted(profiles.items())]

    logging.info(tabulate.tabulate(rows, headers=ProfileRow._fields,
                                   tablefmt='pipe'))


def run_test(args):
    setup_logging(args.verbose)
    gpsbabel.check()

    profile = gpsbabel.resolve_profile(args.name)
    gpsbabel.sanity_check(profile)

    logging.info(f"Reading a little from '{profile.name}' on {profile.device}...")

    options = [part.strip() for part in profile.options.split(",") if part.strip()]

    attempt = gpsbabel.probe(profile.device, profile.format, options)

    if attempt.error:
        raise ValueError(
            f"'{profile.name}' did not answer: {attempt.error}. Run "
            f"'phototripper gpslogger detect' to find its settings again.")

    if not attempt.points:
        logging.warning(
            f"'{profile.name}' answered but had nothing recorded to read. The "
            f"settings look right; the logger looks empty.")
        return

    logging.info(f"'{profile.name}' answers and has a track to give.")


# ---------------------------------------------------------------------------
# extract and wipe
# ---------------------------------------------------------------------------

def preflight(profile, dry_run=False):
    """Look before leaping, unless nothing is going to be leapt.

    A dry run is how you find out what would be sent, which is worth having
    when the logger is still in a bag somewhere. So the checks still run and
    still report, but they only stop a run that was going to touch hardware.
    """
    try:
        gpsbabel.sanity_check(profile)
    except ValueError as error:
        if not dry_run:
            raise
        logging.warning(f"{error}\n  (a dry run reaches no device, carrying on)")


def resolve_output(track):
    """Where the downloaded track goes, without ever overwriting a track."""
    target = os.path.abspath(os.path.expanduser(track or DEFAULT_TRACK_NAME))

    if os.path.isdir(target):
        target = os.path.join(target, DEFAULT_TRACK_NAME)

    return gpsbabel.sequential_name(target)


def extract_track(profile, out_file, args):
    """Download the track, and clear the logger if that was asked for.

    Clearing is folded into the same gpsbabel run, so the logger is only ever
    emptied once the track it held is on disk.
    """
    start = gpsbabel.to_gpsbabel_time(args.date_from) if args.date_from else None
    stop = gpsbabel.to_gpsbabel_time(args.date_to) if args.date_to else None

    wipe = bool(args.wipe)
    if wipe and not args.dry_run:
        wipe = gpsbabel.ask(
            f"Clear the logger '{profile.name}' now that the track is in "
            f"{out_file}?", args.yes, "Not clearing the logger:")

    argv = gpsbabel.build_extract_argv(profile, out_file, wipe=wipe,
                                       start=start, stop=stop)

    if args.dry_run:
        logging.info(f"Dry run, nothing was read: {' '.join(argv)}")
        return None

    logging.info(f"Reading '{profile.name}' into {out_file}...")

    result = gpsbabel.run(argv)

    if result.timed_out:
        raise ValueError(
            f"The logger stopped answering. Nothing was written to {out_file}, "
            f"and nothing was erased.")

    if result.returncode != 0:
        raise ValueError(f"gpsbabel failed: {result.stderr.strip()}")

    points = gpsbabel.read_points(out_file)

    if not points:
        logging.warning(
            f"The logger had nothing recorded: {out_file} holds no track "
            f"points.")
    else:
        logging.info(f"{points} track point(s) written to {out_file}")

    if wipe:
        logging.info(f"The logger '{profile.name}' was cleared.")

    return out_file


def run_extract(args):
    setup_logging(args.verbose)
    gpsbabel.check()

    profile = gpsbabel.resolve_profile(args.logger)
    preflight(profile, args.dry_run)

    extract_track(profile, resolve_output(args.track), args)


def run_wipe(args):
    setup_logging(args.verbose)
    gpsbabel.check()

    profile = gpsbabel.resolve_profile(args.logger)
    preflight(profile, args.dry_run)

    argv = gpsbabel.build_wipe_argv(profile)

    if args.dry_run:
        logging.info(f"Dry run, nothing was erased: {' '.join(argv)}")
        return

    logging.warning(
        f"About to clear '{profile.name}'. Nothing is downloaded first, so "
        f"whatever it still holds is lost.")

    if not gpsbabel.ask(f"Clear the logger '{profile.name}'?", args.yes,
                        "Not clearing the logger:"):
        logging.info("Left alone.")
        return

    result = gpsbabel.run(argv)

    if result.returncode != 0 or result.timed_out:
        raise ValueError(
            f"gpsbabel failed, so the logger may not be cleared: "
            f"{result.stderr.strip() or 'no response'}")

    logging.info(f"The logger '{profile.name}' was cleared.")
