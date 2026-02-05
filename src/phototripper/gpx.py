import logging


def register_args(subparsers):
    parser = subparsers.add_parser(
        'gpx', help='GPX track processing (not yet implemented)'
    )
    parser.set_defaults(func=run)


def run(args):
    logging.warning("The 'gpx' subcommand is not implemented yet.")
