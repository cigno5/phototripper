import os

import pytest

from phototripper import gpsbabel
from phototripper.gpsbabel import Profile

# A real 'gpsbabel -^3' dump, so the parser is held against what the tool
# actually prints rather than against what it was assumed to print.
DUMP = os.path.join(os.path.dirname(__file__), "data", "gpsbabel_formats.txt")

with open(DUMP) as handle:
    FORMATS = gpsbabel.parse_formats(handle.read())


@pytest.fixture(autouse=True)
def formats(monkeypatch):
    """Every lookup answers off the recorded dump, never off the binary."""
    monkeypatch.setattr(gpsbabel, "list_serial_formats", lambda: FORMATS)

GT730 = Profile("gt730", "skytraq", "/dev/ttyUSB0", "baud=230400,initbaud=9600",
                "Renkforce GT-730FL-S")


# ---------------------------------------------------------------------------
# what gpsbabel says it can do
# ---------------------------------------------------------------------------

def test_only_serial_formats_that_read_tracks_are_kept():
    assert "skytraq" in FORMATS
    assert "mtk" in FORMATS
    # m241 declares '--r---': no waypoints, but tracks are what matters here
    assert "m241" in FORMATS

    # 'v900' and 'csv' are files, not something to plug in
    assert "v900" not in FORMATS
    assert "csv" not in FORMATS


def test_a_format_carries_its_options():
    skytraq = FORMATS["skytraq"]

    assert skytraq.description == "SkyTraq Venus based loggers (download)"
    assert skytraq.options["baud"] == "integer"
    assert skytraq.options["erase"] == "boolean"
    assert "last-sector" in skytraq.options


def test_the_erase_spelling_is_read_off_the_format():
    # the whole point of asking gpsbabel: the two families disagree
    venus = ("erase", "erase,no-output")

    assert gpsbabel.erase_options_for("skytraq") == venus
    assert gpsbabel.erase_options_for("miniHomer") == venus
    assert gpsbabel.erase_options_for("mtk") == ("erase", "erase_only")
    assert gpsbabel.erase_options_for("m241") == ("erase", "erase_only")
    assert gpsbabel.erase_options_for("dg-100") == ("erase", "erase_only")


def test_a_format_that_cannot_be_erased_says_so():
    # garmin is a serial format, but nothing on it clears a log
    assert gpsbabel.erase_options_for("garmin") == ("", "")


def test_a_gpsbabel_that_answers_nonsense_is_not_a_crash():
    assert gpsbabel.parse_formats("who knows\nwhat this is\n") == {}


# ---------------------------------------------------------------------------
# the commands
# ---------------------------------------------------------------------------

def test_extract_asks_for_a_gpx_and_erases_nothing():
    argv = gpsbabel.build_extract_argv(GT730, "/tmp/holiday.gpx")

    assert argv == ["gpsbabel", "-t",
                    "-i", "skytraq,baud=230400,initbaud=9600",
                    "-f", "/dev/ttyUSB0",
                    "-o", "gpx", "-F", "/tmp/holiday.gpx"]
    assert "erase" not in argv[3]


def test_wiping_is_folded_into_the_download():
    argv = gpsbabel.build_extract_argv(GT730, "/tmp/holiday.gpx", wipe=True)

    # gpsbabel erases only once it has written the file
    assert argv[3] == "skytraq,baud=230400,initbaud=9600,erase"
    assert argv[-1] == "/tmp/holiday.gpx"


def test_a_time_frame_becomes_a_track_filter():
    argv = gpsbabel.build_extract_argv(
        GT730, "/tmp/holiday.gpx",
        start=gpsbabel.to_gpsbabel_time("2024-06-01"),
        stop=gpsbabel.to_gpsbabel_time("2024-06-02T18:30"))

    assert "-x" in argv
    assert argv[argv.index("-x") + 1] == \
        "track,start=20240601000000,stop=20240602183000"


@pytest.mark.parametrize("text", ["yesterday", "01/06/2024", "2024-13-01"])
def test_an_unreadable_date_says_what_to_type(text):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        gpsbabel.to_gpsbabel_time(text)


def test_wiping_alone_writes_no_file():
    argv = gpsbabel.build_wipe_argv(GT730)

    assert argv == ["gpsbabel", "-t",
                    "-i", "skytraq,baud=230400,initbaud=9600,erase,no-output",
                    "-f", "/dev/ttyUSB0"]
    assert "-F" not in argv


def test_a_logger_that_cannot_be_wiped_on_its_own_says_so():
    # garmin is serial and reads tracks, but nothing on it clears a log
    profile = GT730._replace(format="garmin", options="")

    with pytest.raises(ValueError, match="extract --wipe"):
        gpsbabel.build_wipe_argv(profile)

    with pytest.raises(ValueError, match="--wipe cannot be honoured"):
        gpsbabel.build_extract_argv(profile, "/tmp/out.gpx", wipe=True)


@pytest.mark.parametrize("option", ["erase", "erase_only", "no-output"])
def test_a_probe_refuses_to_carry_a_destructive_option(option):
    # probing talks to hardware nobody has identified yet
    with pytest.raises(ValueError, match="must not pass"):
        gpsbabel.build_probe_argv("/dev/ttyUSB0", "skytraq", [option], "/tmp/o.gpx")


def test_a_probe_carries_the_options_it_was_given():
    argv = gpsbabel.build_probe_argv("/dev/ttyUSB0", "skytraq",
                                     ["baud=230400"], "/tmp/o.gpx")

    assert argv[3] == "skytraq,baud=230400"
    assert argv[-1] == "/tmp/o.gpx"


# ---------------------------------------------------------------------------
# saved profiles
# ---------------------------------------------------------------------------

CONFIG = """\
; a comment that must survive
[gpx]
; another one
max-int-secs = 5340

[logger:gt730]
description = Renkforce GT-730FL-S
format = skytraq
device = /dev/ttyUSB0
options = baud=230400
"""


def test_a_profile_is_read_back_whole(ini):
    ini(CONFIG)

    profile = gpsbabel.load_profile("gt730")

    assert profile.format == "skytraq"
    assert profile.device == "/dev/ttyUSB0"
    assert profile.options == "baud=230400"


def test_a_half_written_profile_is_skipped_with_a_warning(ini, caplog):
    ini("[logger:broken]\ndescription = no format, no device\n")

    assert gpsbabel.list_profiles() == {}
    assert "Ignoring [logger:broken]" in caplog.text


def test_the_only_logger_needs_no_naming(ini):
    ini(CONFIG)

    assert gpsbabel.resolve_profile().name == "gt730"
    assert gpsbabel.resolve_profile("gt730").name == "gt730"


def test_a_choice_of_loggers_has_to_be_made(ini):
    ini(CONFIG + "\n[logger:spare]\nformat = mtk\ndevice = /dev/ttyUSB1\n")

    with pytest.raises(ValueError, match="gt730, spare"):
        gpsbabel.resolve_profile()


def test_no_logger_at_all_points_at_detect(ini):
    ini("[gpx]\n")

    with pytest.raises(ValueError, match="gpslogger detect --save"):
        gpsbabel.resolve_profile()


def test_an_unknown_name_names_the_known_ones(ini):
    ini(CONFIG)

    with pytest.raises(ValueError, match="Known logger\\(s\\): gt730"):
        gpsbabel.load_profile("nope")


def test_saving_a_profile_leaves_the_comments_alone(ini):
    path = ini(CONFIG.replace("[logger:gt730]", "[logger:other]"))

    gpsbabel.save_profile(GT730)

    text = path.read_text()
    assert "; a comment that must survive" in text
    assert "; another one" in text
    assert "[logger:other]" in text

    saved = gpsbabel.load_profile("gt730")
    assert saved.format == "skytraq"
    assert saved.options == "baud=230400,initbaud=9600"


def test_an_existing_profile_is_not_replaced_by_accident(ini):
    ini(CONFIG)

    with pytest.raises(ValueError, match="--force"):
        gpsbabel.save_profile(GT730._replace(device="/dev/ttyUSB9"))

    assert gpsbabel.load_profile("gt730").device == "/dev/ttyUSB0"


def test_forcing_rewrites_only_that_section(ini):
    path = ini(CONFIG)

    gpsbabel.save_profile(GT730._replace(device="/dev/ttyUSB9"), force=True)

    text = path.read_text()
    assert gpsbabel.load_profile("gt730").device == "/dev/ttyUSB9"
    assert "max-int-secs = 5340" in text
    assert "; a comment that must survive" in text
    assert text.count("[logger:gt730]") == 1


# ---------------------------------------------------------------------------
# looking before leaping
# ---------------------------------------------------------------------------

def test_a_missing_port_is_reported_before_anything_is_sent(tmp_path):
    profile = GT730._replace(device=str(tmp_path / "not-there"))

    with pytest.raises(ValueError, match="plugged in"):
        gpsbabel.sanity_check(profile)


def test_a_port_that_is_not_a_port_is_reported(tmp_path):
    plain = tmp_path / "plain.txt"
    plain.write_text("not a serial port")

    with pytest.raises(ValueError, match="not a serial port"):
        gpsbabel.sanity_check(GT730._replace(device=str(plain)))


def test_an_unknown_format_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="skytraq"):
        gpsbabel.sanity_check(GT730._replace(format="invented"))


def test_an_option_the_format_never_heard_of_is_caught():
    profile = GT730._replace(options="baud=230400,turbo=1")

    with pytest.raises(ValueError, match="does not accept turbo"):
        gpsbabel.sanity_check(profile)


# ---------------------------------------------------------------------------
# naming the file
# ---------------------------------------------------------------------------

def test_a_free_name_is_used_as_is(tmp_path):
    target = str(tmp_path / "track.gpx")

    assert gpsbabel.sequential_name(target) == target


def test_a_taken_name_steps_up(tmp_path):
    (tmp_path / "track.gpx").write_text("")
    (tmp_path / "track-1.gpx").write_text("")

    assert gpsbabel.sequential_name(str(tmp_path / "track.gpx")) == \
        str(tmp_path / "track-2.gpx")


# ---------------------------------------------------------------------------
# ports
# ---------------------------------------------------------------------------

def test_ports_are_named_by_id_and_described_from_sysfs(tmp_path, monkeypatch):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyUSB0").write_text("")

    by_id = dev / "serial" / "by-id"
    by_id.mkdir(parents=True)
    (by_id / "usb-Prolific_PL2303-if00-port0").symlink_to(dev / "ttyUSB0")

    # the real shape: /sys/class/tty/ttyUSB0/device is a symlink to the USB
    # *interface*, and the vendor lives on the device above it
    usb = tmp_path / "sys" / "devices" / "usb1" / "1-1"
    interface = usb / "1-1:1.0"
    interface.mkdir(parents=True)
    for name, value in (("idVendor", "067b"), ("idProduct", "2303"),
                        ("manufacturer", "Prolific"), ("product", "PL2303")):
        (usb / name).write_text(value + "\n")

    tty = tmp_path / "sys" / "class" / "tty" / "ttyUSB0"
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(interface)

    monkeypatch.setattr(gpsbabel, "BY_ID_DIR", str(by_id))
    monkeypatch.setattr(gpsbabel, "TTY_GLOBS", (str(dev / "ttyUSB*"),))
    monkeypatch.setattr(gpsbabel, "SYS_TTY_DIR",
                        str(tmp_path / "sys" / "class" / "tty"))

    ports = gpsbabel.list_serial_ports()

    assert len(ports) == 1
    port = ports[0]
    # the by-id name is the one that survives a reboot, so it is the one kept
    assert port.device.endswith("usb-Prolific_PL2303-if00-port0")
    # ...and the short name is kept alongside it, since that is the one a
    # person recognises
    assert port.tty == str(dev / "ttyUSB0")
    assert port.usb_id == "067b:2303"
    assert port.product == "PL2303"


def test_a_port_sysfs_knows_nothing_about_is_still_listed(tmp_path, monkeypatch):
    dev = tmp_path / "dev"
    dev.mkdir()
    (dev / "ttyACM0").write_text("")

    monkeypatch.setattr(gpsbabel, "BY_ID_DIR", str(tmp_path / "absent"))
    monkeypatch.setattr(gpsbabel, "TTY_GLOBS", (str(dev / "ttyACM*"),))
    monkeypatch.setattr(gpsbabel, "SYS_TTY_DIR", str(tmp_path / "absent"))

    ports = gpsbabel.list_serial_ports()

    assert [port.device for port in ports] == [str(dev / "ttyACM0")]
    # with no by-id name to prefer, the two are simply the same
    assert ports[0].tty == ports[0].device
    assert ports[0].usb_id is None
