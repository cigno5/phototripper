# phototripper

A CLI tool that sorts and organizes RAW photo files using EXIF metadata and geolocation.

## Geotagging from a GPS logger

`gpx` writes coordinates onto pictures by matching their shooting time against a
GPS track. The track can come from a file, or straight off a GPS logger.

Reading a logger needs [gpsbabel](https://www.gpsbabel.org/), which has to be
told the format, the port and the baud rate. You do not have to know any of
them: plug the logger in and let `gpslogger` work it out.

```bash
# probe every serial port, read-only, and record what answered
phototripper gpslogger detect --save gt730

# from then on the logger is known by name
phototripper gpslogger list
phototripper gpslogger extract -t ~/tracks/holiday.gpx
```

`detect` asks before it opens anything, because opening a serial port asserts
DTR/RTS and that reboots Arduino- and ESP32-class boards, whatever they were
busy with. `--device` probes just one port, and `--yes` skips the question.

With a logger configured, `gpx` can fetch the track itself:

```bash
# read the logger into <shoot>/track.gpx, then geotag the pictures with it
phototripper gpx -s ~/shoot --extract

# ...and clear the logger, once the track it held is safely on disk
phototripper gpx -s ~/shoot --extract --wipe
```

`--wipe` folds the erase into the same gpsbabel run, so nothing is ever lost
that was not just written. To clear a logger without reading it first, ask for
it plainly with `phototripper gpslogger wipe`; both prompt before erasing, and
both refuse when there is no terminal to ask at.

`-t` is a name rather than a path: an unqualified one sits beside the pictures,
and left out entirely it is `track.gpx` there. Extracting never overwrites a
track — a name already taken becomes `track-1.gpx`.

## Development setup

Requires Python 3.14 and [Poetry](https://python-poetry.org/).

```bash
poetry install
```

If you use **conda**, a dedicated environment is provided:

```bash
conda activate phototripper
poetry install
```
