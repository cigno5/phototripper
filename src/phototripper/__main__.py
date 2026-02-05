import argparse
import sys

from . import gpx, sort


def main():
    parser = argparse.ArgumentParser(prog='phototripper')
    subparsers = parser.add_subparsers(dest='command')

    sort.register_args(subparsers)
    gpx.register_args(subparsers)

    args = parser.parse_args()

    if not hasattr(args, 'func') or args.func is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
