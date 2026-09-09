import argparse
import sys

from . import config, gpx, sort
from .common import apply_config_to_args


def main():
    parser = argparse.ArgumentParser(prog='phototripper')
    subparsers = parser.add_subparsers(dest='command')

    sort.register_args(subparsers)
    gpx.register_args(subparsers)
    config.register_args(subparsers)

    args = parser.parse_args()

    if not hasattr(args, 'func') or args.func is None:
        parser.print_help()
        sys.exit(1)

    apply_config_to_args(args, args.command)

    try:
        args.func(args)
    except ValueError as error:
        # the subcommands raise ValueError for anything the user can fix
        print(f"{parser.prog}: {error}", file=sys.stderr)
        sys.exit(2)


if __name__ == '__main__':
    main()
