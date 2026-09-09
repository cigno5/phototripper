import importlib.resources
import os
import shutil

from .common import (
    APP_CONFIG_FILENAME,
    _find_config_file,
    load_app_config,
    registered_modules,
)


def register_args(subparsers):
    parser = subparsers.add_parser(
        "config", help="Manage phototripper configuration"
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Copy the reference config to ~/.config/phototripper.ini",
    )
    parser.add_argument(
        "--show",
        nargs="?",
        const="common",
        metavar="MODULE",
        help="Show effective merged config (optionally for a specific module)",
    )
    parser.set_defaults(func=run)


def run(args):
    if args.init:
        _do_init()
    elif args.show is not None:
        _do_show(args.show)
    else:
        # No flag given — print help-like info
        print(f"Config file: {_find_config_file() or '(none found)'}")
        print("Use --init to create a config file, or --show to display effective settings.")


def _do_init():
    dest_dir = os.path.join(os.path.expanduser("~"), ".config")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, APP_CONFIG_FILENAME)

    if os.path.exists(dest):
        print(f"Config file already exists: {dest}")
        return

    template = importlib.resources.files("phototripper.templates").joinpath(
        APP_CONFIG_FILENAME
    )
    with importlib.resources.as_file(template) as src:
        shutil.copy2(src, dest)

    print(f"Config file created: {dest}")


def _do_show(module_name):
    path = _find_config_file()
    if path:
        print(f"Config file: {path}")
    else:
        print("No config file found (using hardcoded defaults).")

    module_name = module_name if module_name != "common" else None
    if module_name and module_name not in registered_modules():
        known = ", ".join(["common", *registered_modules()])
        print(f"\nUnknown module '{module_name}'. Known modules: {known}")
        return

    config = load_app_config(module_name)

    _show_loggers()

    print()
    header = f"[{module_name}]" if module_name else "[common]"
    print(f"Effective settings for {header}:")
    for ini_key in sorted(config):
        print(f"  {ini_key.replace('-', '_'):20s} = {config[ini_key]}")


def _show_loggers():
    """Name the configured GPS loggers.

    They live in [logger:<name>] sections rather than a module's own, so
    load_app_config never sees them and 'config --show' would otherwise give
    no hint that they are there.
    """
    from .gpsbabel import list_profiles

    profiles = list_profiles()
    if profiles:
        print(f"\nGPS loggers: {', '.join(sorted(profiles))} "
              f"(see 'phototripper gpslogger list')")
