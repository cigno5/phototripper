import argparse
import sys
from . import sort

def main():
    parser = argparse.ArgumentParser(prog="phototripper")
    subparsers = parser.add_subparsers(title='subcommands', dest='command', required=True)

    sort.configure_parser(subparsers)

    # Future subcommands (e.g. gpx) will be added here

    args = parser.parse_args()
    if hasattr(args, 'func'):
        try:
            args.func(args)
        except Exception as e:
            # We print the error but also the traceback for debugging since this is a dev tool
            print(f"Error: {e}", file=sys.stderr)
            # import traceback
            # traceback.print_exc()
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == '__main__':
    main()