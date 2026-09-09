import argparse
import logging
import os

import pytest

from phototripper import gpsbabel, gpslogger

from .conftest import MINIMAL_GPX


# What a gpsbabel that knows the usual loggers reports.
FORMATS = {
    "skytraq": gpsbabel.Format("skytraq", "SkyTraq Venus", {
        "baud": "integer", "initbaud": "integer", "last-sector": "integer",
        "erase": "boolean", "no-output": "boolean"}),
    "mtk": gpsbabel.Format("mtk", "MTK", {
        "baud": "integer", "erase": "boolean", "erase_only": "boolean"}),
}


@pytest.fixture(autouse=True)
def formats(monkeypatch):
    """A gpsbabel that knows the usual loggers, unless a test says otherwise."""
    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: FORMATS)


class Args:
    """Stand-in for the argparse namespace of any gpslogger verb."""

    def __init__(self, **kwargs):
        self.__dict__.update(
            verbose=None, dry_run=None, logger=None, yes=None, name=None,
            track=None, date_from=None, date_to=None, wipe=None,
            save=None, force=None, device=None,
        )
        self.__dict__.update(kwargs)


CONFIG = """\
[logger:gt730]
description = Renkforce GT-730FL-S
format = skytraq
device = {device}
options = baud=230400
"""


@pytest.fixture
def port(tmp_path, monkeypatch):
    """A stand-in for the logger's serial port that sanity_check will accept."""
    device = "/dev/null"  # a character device that exists everywhere

    monkeypatch.setattr(gpsbabel, "list_serial_ports",
                        lambda: [gpsbabel.Port(device, device, "067b:2303",
                                               "Prolific", "PL2303")])
    return device


@pytest.fixture
def gps(monkeypatch):
    """Answer every gpsbabel invocation with a canned result.

    The device is never there in a test, so what is asserted is the command
    that would have been sent and the file that would have come back.
    """
    calls = []

    def _install(points=3, returncode=0):
        def fake_run(argv, timeout=None):
            calls.append(argv)

            if "-F" in argv and points:
                out = argv[argv.index("-F") + 1]
                with open(out, "w") as handle:
                    handle.write(MINIMAL_GPX)

            return gpsbabel.Result(argv, returncode, "", "", False)

        monkeypatch.setattr(gpsbabel, "run", fake_run)
        monkeypatch.setattr(gpsbabel, "check", lambda: None)
        return calls

    _install.calls = calls
    return _install


def touched_device(calls):
    """The calls that actually went to a port, not the capability queries."""
    return [argv for argv in calls if "-f" in argv]


@pytest.fixture
def told(caplog):
    """Everything the run printed."""
    def _read():
        return "\n".join(record.getMessage() for record in caplog.records)

    caplog.set_level(logging.DEBUG)
    return _read


# ---------------------------------------------------------------------------
# detect
# ---------------------------------------------------------------------------

def test_the_probe_stops_at_the_first_format_that_answers(port, gps, told,
                                                          monkeypatch):
    formats = {
        "skytraq": gpsbabel.Format("skytraq", "SkyTraq", {"baud": "integer",
                                                          "last-sector": "integer",
                                                          "erase": "boolean",
                                                          "no-output": "boolean"}),
        "mtk": gpsbabel.Format("mtk", "MTK", {"erase": "boolean",
                                              "erase_only": "boolean"}),
    }
    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: formats)

    calls = gps(points=3)
    gpslogger.run_detect(Args(yes=True))

    # skytraq leads the hint list, and it answers at the first baud
    assert len(calls) == 1
    assert calls[0][3] == "skytraq,baud=230400"
    assert "1214" not in told()
    assert "skytraq" in told()


def test_a_format_that_says_nothing_is_tried_and_reported(port, gps, told,
                                                          monkeypatch):
    formats = {
        "mtk": gpsbabel.Format("mtk", "MTK", {"erase": "boolean"}),
        "skytraq": gpsbabel.Format("skytraq", "SkyTraq", {"baud": "integer",
                                                          "last-sector": "integer"}),
    }
    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: formats)

    answers = iter([0, 0, 3])
    calls = []

    def fake_run(argv, timeout=None):
        calls.append(argv)
        points = next(answers, 0)
        if points:
            out = argv[argv.index("-F") + 1]
            with open(out, "w") as handle:
                handle.write(MINIMAL_GPX)
        return gpsbabel.Result(argv, 0, "", "", False)

    monkeypatch.setattr(gpsbabel, "run", fake_run)
    monkeypatch.setattr(gpsbabel, "check", lambda: None)

    gpslogger.run_detect(Args(yes=True))

    out = told()
    assert "answered, nothing recorded" in out
    assert len(calls) == 3


def test_an_empty_logger_is_still_a_logger(port, gps, told, monkeypatch):
    """A clean answer is what says the format was right.

    Whether the logger happens to hold a trip is a different question: a
    logger could otherwise only be set up while it had one on it.
    """
    gps(points=0)

    gpslogger.run_detect(Args(yes=True))

    out = told()
    assert "answered, nothing recorded" in out
    assert "Best: gpsbabel -t -i skytraq" in out


def test_a_track_beats_a_bare_answer(port, gps, told, monkeypatch):
    """The fallback must not win over a format that actually read something."""

    calls = []

    def fake_run(argv, timeout=None):
        calls.append(argv)
        # everything answers; only the third baud has a track behind it
        if len(calls) == 3:
            with open(argv[argv.index("-F") + 1], "w") as handle:
                handle.write(MINIMAL_GPX)
        return gpsbabel.Result(argv, 0, "", "", False)

    monkeypatch.setattr(gpsbabel, "run", fake_run)
    monkeypatch.setattr(gpsbabel, "check", lambda: None)

    gpslogger.run_detect(Args(yes=True))

    assert "Best: gpsbabel -t -i skytraq,baud=57600" in told()


def test_no_probe_ever_carries_a_destructive_option(port, gps, monkeypatch):
    formats = {name: gpsbabel.Format(
        name, name, {"baud": "integer", "last-sector": "integer",
                     "erase": "boolean", "erase_only": "boolean",
                     "no-output": "boolean"})
        for name in ("skytraq", "mtk", "m241")}
    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: formats)

    calls = gps(points=0, returncode=1)

    with pytest.raises(ValueError, match="No GPS logger answered"):
        gpslogger.run_detect(Args(yes=True))

    assert calls, "nothing was probed at all"
    for argv in calls:
        spec = argv[argv.index("-i") + 1]
        for destructive in gpsbabel.DESTRUCTIVE_OPTIONS:
            assert destructive not in spec.split(",")


def test_a_logger_that_never_appeared_is_not_blamed_for_refusing(port, gps,
                                                                 monkeypatch):
    """The two failures want opposite fixes, so the ports tried are named."""
    gps(points=0, returncode=1)

    with pytest.raises(ValueError) as raised:
        gpslogger.run_detect(Args(yes=True))

    message = str(raised.value)
    assert "/dev/null" in message, "say which port was actually tried"
    assert "not in that list" in message
    assert "lsusb" in message
    assert "dialout" in message


def test_what_worked_is_offered_as_a_command(port, gps, told, monkeypatch):
    gps(points=3)

    gpslogger.run_detect(Args(yes=True))

    out = told()
    assert "Best: gpsbabel -t -i skytraq,baud=230400" in out
    assert "--save" in out


def test_saving_records_what_the_probe_found(port, gps, ini):
    ini("[gpx]\n")
    gps(points=3)

    gpslogger.run_detect(Args(yes=True, save="gt730"))

    saved = gpsbabel.load_profile("gt730")
    assert saved.format == "skytraq"
    assert saved.options == "baud=230400"

    # how it erases is not recorded: gpsbabel is asked when it matters
    assert "erase" not in "".join(saved)
    assert gpsbabel.build_wipe_argv(saved)[3].endswith("erase,no-output")


def test_a_named_device_is_the_only_one_probed(gps, told, monkeypatch):
    monkeypatch.setattr(
        gpsbabel, "list_serial_ports",
        lambda: [gpsbabel.Port(f"/dev/tty{n}", f"/dev/tty{n}", None, None, None)
                 for n in ("USB0", "ACM0")])

    calls = gps(points=3)
    gpslogger.run_detect(Args(yes=True, device="/dev/ttyUSB7"))

    assert {argv[argv.index("-f") + 1] for argv in calls} == {"/dev/ttyUSB7"}


def test_several_ports_are_called_out_before_they_are_probed(gps, told,
                                                             monkeypatch):
    monkeypatch.setattr(
        gpsbabel, "list_serial_ports",
        lambda: [gpsbabel.Port("/dev/ttyUSB0", "/dev/ttyUSB0", "067b:2303",
                               "Prolific", "PL2303"),
                 gpsbabel.Port("/dev/ttyACM0", "/dev/ttyACM0", "0483:5740",
                               "ST", "STM32")])
    gps(points=3)

    gpslogger.run_detect(Args(yes=True))

    out = told()
    assert "/dev/ttyACM0" in out
    assert "--device" in out


def test_the_table_shows_the_short_name_and_maps_it_to_the_stable_one(
        gps, told, monkeypatch):
    """A by-id path is 80 characters and unreadable in a column, but it is
    what gets saved, so both have to be visible."""
    monkeypatch.setattr(
        gpsbabel, "list_serial_ports",
        lambda: [gpsbabel.Port("/dev/serial/by-id/usb-Canmore_GT-730-if00",
                               "/dev/ttyACM0", "0483:5740", "ST", "COM Port")])
    gps(points=3)

    gpslogger.run_detect(Args(yes=True))

    out = told()
    assert "| /dev/ttyACM0" in out, "the column carries the recognisable name"
    assert "/dev/ttyACM0 -> /dev/serial/by-id/usb-Canmore_GT-730-if00" in out
    assert "survives a reboot" in out
    # and the stable name is what actually gets used
    assert "-f /dev/serial/by-id/usb-Canmore_GT-730-if00" in out


def test_a_port_with_no_stable_name_is_not_padded_with_noise(gps, told,
                                                             monkeypatch):
    monkeypatch.setattr(
        gpsbabel, "list_serial_ports",
        lambda: [gpsbabel.Port("/dev/ttyUSB0", "/dev/ttyUSB0", None, None, None)])
    gps(points=3)

    gpslogger.run_detect(Args(yes=True))

    assert "survives a reboot" not in told()


def test_nothing_is_opened_without_being_asked(port, gps, told, monkeypatch):
    """Opening a port reboots dev boards, so consent comes before the probe."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    calls = gps(points=3)

    gpslogger.run_detect(Args())

    out = told()
    assert touched_device(calls) == [], "not a single port was opened"
    assert "reboots Arduino and ESP32 class boards" in out
    assert "Nothing was probed" in out


def test_nothing_plugged_in_says_so(gps, monkeypatch):
    monkeypatch.setattr(gpsbabel, "list_serial_ports", list)

    with pytest.raises(ValueError, match="No serial port found"):
        gpslogger.run_detect(Args(yes=True))


# ---------------------------------------------------------------------------
# list and test
# ---------------------------------------------------------------------------

def test_list_shows_what_is_configured(ini, told):
    ini(CONFIG.format(device="/dev/ttyUSB0"))

    gpslogger.run_list(Args())

    out = told()
    assert "gt730" in out
    assert "skytraq" in out
    assert "Renkforce GT-730FL-S" in out


def test_list_with_nothing_configured_points_at_detect(ini, told):
    ini("[gpx]\n")

    gpslogger.run_list(Args())

    assert "gpslogger detect --save" in told()


def test_test_reports_a_logger_that_answers_empty(ini, port, gps, told):
    ini(CONFIG.format(device=port))
    gps(points=0)

    gpslogger.run_test(Args())

    assert "looks empty" in told()


def test_test_names_the_logger_that_stopped_answering(ini, port, monkeypatch):
    ini(CONFIG.format(device=port))
    monkeypatch.setattr(gpsbabel, "check", lambda: None)
    monkeypatch.setattr(
        gpsbabel, "run",
        lambda argv, timeout=None: gpsbabel.Result(argv, 1, "", "port busy", False))

    with pytest.raises(ValueError, match="did not answer"):
        gpslogger.run_test(Args())


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------

def test_extract_writes_the_track_and_counts_it(ini, port, gps, told, tmp_path):
    ini(CONFIG.format(device=port))
    target = tmp_path / "holiday.gpx"
    gps(points=3)

    gpslogger.run_extract(Args(track=str(target)))

    assert target.exists()
    assert "3 track point(s)" in told()


def test_extract_never_overwrites_a_track(ini, port, gps, tmp_path):
    ini(CONFIG.format(device=port))
    (tmp_path / "track.gpx").write_text("an earlier trip")
    gps(points=3)

    gpslogger.run_extract(Args(track=str(tmp_path)))

    assert (tmp_path / "track.gpx").read_text() == "an earlier trip"
    assert (tmp_path / "track-1.gpx").exists()


def test_a_time_frame_reaches_gpsbabel(ini, port, gps, tmp_path):
    ini(CONFIG.format(device=port))
    calls = gps(points=3)

    gpslogger.run_extract(Args(track=str(tmp_path / "t.gpx"),
                               date_from="2024-06-01", date_to="2024-06-02"))

    argv = calls[0]
    assert argv[argv.index("-x") + 1] == \
        "track,start=20240601000000,stop=20240602000000"


def test_a_dry_run_shows_the_command_and_reads_nothing(ini, port, gps, told,
                                                       tmp_path):
    ini(CONFIG.format(device=port))
    calls = gps(points=3)

    gpslogger.run_extract(Args(track=str(tmp_path / "t.gpx"), dry_run=True))

    assert calls == []
    assert not (tmp_path / "t.gpx").exists()
    assert "Dry run" in told()
    assert "gpsbabel -t -i skytraq" in told()


def test_extract_wipes_only_when_it_is_allowed_to(ini, port, gps, told,
                                                  tmp_path, monkeypatch):
    ini(CONFIG.format(device=port))
    calls = gps(points=3)

    # off a terminal there is nobody to ask, so the answer is no
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    gpslogger.run_extract(Args(track=str(tmp_path / "a.gpx"), wipe=True))

    assert "erase" not in calls[0][3]
    assert "Pass --yes" in told()

    gpslogger.run_extract(Args(track=str(tmp_path / "b.gpx"), wipe=True, yes=True))

    assert calls[1][3].endswith(",erase")
    assert "was cleared" in told()


def test_a_logger_that_says_nothing_leaves_no_half_file(ini, port, told,
                                                        tmp_path, monkeypatch):
    ini(CONFIG.format(device=port))
    monkeypatch.setattr(gpsbabel, "check", lambda: None)
    monkeypatch.setattr(
        gpsbabel, "run",
        lambda argv, timeout=None: gpsbabel.Result(argv, None, "", "", True))

    with pytest.raises(ValueError, match="nothing was erased"):
        gpslogger.run_extract(Args(track=str(tmp_path / "t.gpx"), wipe=True))


# ---------------------------------------------------------------------------
# wipe
# ---------------------------------------------------------------------------

def test_wipe_asks_first_and_takes_silence_for_a_no(ini, port, gps, told,
                                                    monkeypatch):
    ini(CONFIG.format(device=port))
    calls = gps(points=0)

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    gpslogger.run_wipe(Args())

    assert calls == []
    assert "is lost" in told()
    assert "Left alone" in told()


def test_wipe_downloads_nothing(ini, port, gps, told, monkeypatch):
    ini(CONFIG.format(device=port))
    calls = gps(points=0)

    gpslogger.run_wipe(Args(yes=True))

    assert calls[0][3] == "skytraq,baud=230400,erase,no-output"
    assert "-F" not in calls[0]
    assert "was cleared" in told()


def test_a_dry_run_shows_the_command_with_the_logger_still_in_the_bag(
        ini, gps, told, tmp_path):
    """The dry run is how you check the command; it should not need hardware."""
    ini(CONFIG.format(device=str(tmp_path / "not-plugged-in")))
    calls = gps(points=0)

    gpslogger.run_wipe(Args(dry_run=True))
    gpslogger.run_extract(Args(dry_run=True, track=str(tmp_path / "t.gpx")))

    out = told()
    assert touched_device(calls) == []
    assert "is not there" in out, "the problem is still reported"
    assert "a dry run reaches no device" in out
    assert "erase,no-output" in out


def test_without_a_dry_run_a_missing_port_stops_everything(ini, gps, tmp_path):
    ini(CONFIG.format(device=str(tmp_path / "not-plugged-in")))
    calls = gps(points=0)

    with pytest.raises(ValueError, match="is not there"):
        gpslogger.run_wipe(Args(yes=True))

    assert touched_device(calls) == []


def test_a_dry_run_never_erases(ini, port, gps, told):
    ini(CONFIG.format(device=port))
    calls = gps(points=0)

    gpslogger.run_wipe(Args(dry_run=True, yes=True))

    assert calls == []
    assert "nothing was erased" in told()


# ---------------------------------------------------------------------------
# the shape of the subcommand
# ---------------------------------------------------------------------------

def test_no_verb_is_refused():
    parser = argparse.ArgumentParser()
    gpslogger.register_args(parser.add_subparsers(dest='command'))

    with pytest.raises(SystemExit):
        parser.parse_args(["gpslogger"])


def test_the_default_track_lands_where_you_are(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert gpslogger.resolve_output(None) == str(tmp_path / "track.gpx")
    assert gpslogger.resolve_output("holiday.gpx") == str(tmp_path / "holiday.gpx")


def test_a_folder_gets_the_default_name(tmp_path):
    assert gpslogger.resolve_output(str(tmp_path)) == \
        os.path.join(str(tmp_path), "track.gpx")
