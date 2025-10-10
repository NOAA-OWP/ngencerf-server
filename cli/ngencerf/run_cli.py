#!/usr/bin/env python3
"""
ngencerf CLI entry point.

Supports import, export, show, run, delete, cancel, register, and download commands
for managing calibration jobs via a REST API.
"""

import argparse
import sys

from ngencerf.cli_functions import (
    import_job,
    update_job,
    run_job,
    delete_job,
    cancel_job,
    handle_export_display,
    list_jobs,
    upload_observational_data,
    upload_forcing_data,
    upload_geopackage_data,
    download_zip, archive_job, unarchive_job, about, generate_regionalization_files, job_status, update_and_get_gage_status,
)
from ngencerf.cli_user import ngen_login, ngen_register


class SmartArgumentParser(argparse.ArgumentParser):
    """
    Custom ArgumentParser that correctly formats subcommand usage without duplicating the full command list.

    This subclass provides better error messages for unknown commands and
    automatically registers subparsers to avoid duplication.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._subparsers: None | argparse._SubParsersAction = None
        self.hidden_commands: set[str] = set()  # track hidden subcommands

    def set_subparsers(self, subparsers_action: argparse._SubParsersAction) -> None:
        """
        Store a reference to the subparsers action for later use in error handling.

        This method is necessary because argparse does not directly expose the subparsers
        once created, making it difficult to provide custom error handling for unknown commands.

        :param subparsers_action: The argparse._SubParsersAction object created by add_subparsers().
        :raises TypeError: If the provided action is not an instance of argparse._SubParsersAction.
        """
        if not isinstance(subparsers_action, argparse._SubParsersAction):
            raise TypeError(f"Expected _SubParsersAction, got {type(subparsers_action)}")
        self._subparsers = subparsers_action  # type: ignore

    def format_usage(self):
        """
        Customize the usage message to show only the subcommand syntax.

        This makes the usage message more concise by stripping the full program path
        and showing only the relevant subcommand syntax.
        """
        usage = super().format_usage()
        prog = self.prog.split()[-1]  # Only keep the subcommand part
        if prog:
            usage = usage.replace(self.prog, prog, 1)
        return usage.strip()

    def error(self, message):
        """
        Override the default error handling to provide more user-friendly messages.

        This method handles unknown commands gracefully, displaying a concise error
        message without the full traceback.
        """
        # Extract the command name if present
        cmd = sys.argv[1] if len(sys.argv) > 1 else None

        # Handle unknown commands
        if self._subparsers and cmd not in self._subparsers._name_parser_map:
            print(f"\nerror: unknown command '{cmd}'\n", flush=True)
            # Show only visible (non-hidden) commands
            visible = [
                name for name in self._subparsers._name_parser_map.keys()
                if name not in getattr(self, "hidden_commands", set())
            ]
            if visible:
                print("valid commands:\n  " + "\n  ".join(sorted(visible)), flush=True)
            # Optionally still show generic help (already omits hidden via SUPPRESS)
            # self.print_help()
            self.exit(2)

        # Default error handling for known commands
        print(f"\nerror: {message}\n", flush=True)
        self.print_help()
        self.exit(2)

    def print_help(self, file=None):
        if self._subparsers and hasattr(self._subparsers, "_choices_actions"):
            orig = list(self._subparsers._choices_actions)
            try:
                self._subparsers._choices_actions = [
                    a for a in orig
                    # hide explicitly-hidden commands
                    if getattr(a, "name", None) not in self.hidden_commands
                       # and also hide anything whose help was SUPPRESS (== '==SUPPRESS==')
                       and getattr(a, "help", None) != argparse.SUPPRESS
                ]
                return super().print_help(file=file)
            finally:
                self._subparsers._choices_actions = orig
        else:
            return super().print_help()


def str_to_bool(value):
    """
    Convert a string representation of truth to True or False.

    True values are 'true', 't', '1', 'yes', 'y'.
    False values are 'false', 'f', '0', 'no', 'n'.

    Raises:
        argparse.ArgumentTypeError: If the value is not a recognized boolean string.
    """
    if isinstance(value, bool):
        return value
    val = value.lower()
    if val in {'true', '1', 'yes', 'y'}:
        return True
    elif val in {'false', '0', 'no', 'n'}:
        return False
    else:
        raise argparse.ArgumentTypeError(f"Invalid boolean value: '{value}'")


# Commands that do not require authentication
COMMANDS_AUTH_EXEMPT = {"register"}


def main():
    """
    Main entry point for the ngencerf CLI.

    Parses command-line arguments, handles authentication,
    and dispatches to the appropriate command handler.
    """
    parser = SmartArgumentParser(
        prog="ngencerf",
        description="ngencerf CLI tool",
        formatter_class=argparse.HelpFormatter,
        conflict_handler='resolve',
        usage="ngencerf [command] [options]"
    )

    # Register subparsers and set the reference on the main parser
    subparsers = parser.add_subparsers(
        dest="command",
        title="commands",
        metavar="",
        required=True
    )

    # Attach the subparsers to the main parser
    parser.set_subparsers(subparsers)

    def add_parser(name, help_text, *, hidden: bool = False):
        """
        Create a subparser for a specific command.

        :param name: The name of the subcommand (e.g., 'import', 'update').
        :param help_text: The description text for the subcommand.
        :param hidden: If True, hide this command from top-level --help but keep its own help.
        :return: The created subparser.
        """
        # Create the subparser without the default help action
        subparser = parser._subparsers.add_parser(
            name,
            help=(argparse.SUPPRESS if hidden else help_text),
            add_help=False,  # Prevent the default -h/--help conflict
            description=help_text,  # always show description when running `subcmd --help`
            formatter_class=argparse.HelpFormatter,
            conflict_handler='resolve',  # Avoid conflict when adding the help action
            usage=f"{parser.prog} {name} [-h] [--flags] <args>"
        )

        # re-add -h/--help cleanly
        help_actions = [a for a in subparser._actions if isinstance(a, argparse._HelpAction)]
        for action in help_actions:
            subparser._remove_action(action)

        # Add the custom help action back
        subparser.add_argument('-h', '--help', action='help', help='show this help message and exit')

        # Track hidden for help/error filtering
        if hidden:
            parser.hidden_commands.add(name)
        return subparser

    # Registering all the subcommands
    about_parser = add_parser("about", "Shows releases of the various server components")
    about_parser.add_argument(
        "--output", "-o",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",  # Use the sentinel value
        default="__DEFAULT__",
        help="Path to save the 'about' output (optional output path)"
    )
    about_parser.set_defaults(func=lambda cmd_args: about(output_path=cmd_args.output_path))

    archive_parser = add_parser("archive", "Archive one or more jobs")
    archive_parser.add_argument(
        "run_ids",
        type=int,
        nargs="+",  # One or more space-separated integers
        help="One or more calibration job IDs"
    )
    archive_parser.set_defaults(func=lambda cmd_args: archive_job(cmd_args.run_ids))

    cancel_parser = add_parser("cancel", "Cancel job")
    cancel_parser.add_argument("run_id", type=int, help="Calibration job ID")
    cancel_parser.set_defaults(func=lambda cmd_args: cancel_job(cmd_args.run_id))

    delete_parser = add_parser("delete", "Delete job")
    delete_parser.add_argument(
        "run_ids",
        type=int,
        nargs="+",  # One or more space-separated integers
        help="One or more calibration job IDs"
    )
    delete_parser.set_defaults(func=lambda cmd_args: delete_job(cmd_args.run_ids))

    download_parser = add_parser("download", "Download ZIP file for a calibration job")
    download_parser.add_argument("run_id", type=int, help="Calibration job ID")
    download_parser.add_argument(
        "--output", "-o",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",  # Use the sentinel value
        default="__DEFAULT__",
        help="Path to save ZIP file or directory (optional output path)"
    )
    download_parser.set_defaults(func=lambda cmd_args: download_zip(cmd_args.run_id, output_path=cmd_args.output_path))

    export_parser = add_parser("export", "Export job to JSON")
    export_parser.add_argument("run_id", type=int, help="Calibration job ID")
    export_parser.add_argument(
        "--output", "-o",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",  # Use the sentinel value
        default="__DEFAULT__",
        help="Path to save file or directory (optional output path)"
    )
    export_parser.add_argument(
        "--show", "-s",
        dest="show",
        nargs="?",
        const=True,
        default=False,
        type=str_to_bool,
        help="Also display the job (default: False)"
    )
    export_parser.set_defaults(func=lambda cmd_args: handle_export_display(
        calibration_run_id=cmd_args.run_id,
        output_path=cmd_args.output_path,
        display=cmd_args.show
    ))

    forcing_parser = add_parser("upload-forcing", "Upload a directory of forcing files for a calibration job")
    forcing_parser.add_argument("run_id", type=int, help="Calibration job ID")
    forcing_parser.add_argument("forcing_dir", help="Path to directory containing forcing files")
    forcing_parser.set_defaults(func=lambda cmd_args: upload_forcing_data(cmd_args.forcing_dir, cmd_args.run_id))

    gage_status_parser = add_parser("gage-status", "Query or update gage active status", hidden=True)
    gage_status_parser.add_argument("gage_id", type=str, help="Gage id")
    gage_status_parser.add_argument(
        "is_active",
        type=str_to_bool,
        nargs="?",
        help="Desired active state (true/false). If omitted, just query current status."
    )
    gage_status_parser.set_defaults(func=lambda cmd_args: update_and_get_gage_status(cmd_args.gage_id, cmd_args.is_active))

    gpkg_parser = add_parser("upload-geopkg", "Upload a GPKG file for a calibration job")
    gpkg_parser.add_argument("run_id", type=int, help="Calibration job ID")
    gpkg_parser.add_argument("gpkg_file", help="Path to the geopackage (.gpkg) file")
    gpkg_parser.set_defaults(func=lambda cmd_args: upload_geopackage_data(cmd_args.gpkg_file, cmd_args.run_id))

    import_parser = add_parser("import", "Create job from a JSON file")
    import_parser.add_argument("input_file", help="Path to the JSON file")
    import_parser.add_argument(
        "--run", "-r",
        dest="run_after_import",
        nargs="?",
        type=str_to_bool,
        const=True,  # Default to True if specified without a value
        default=None,
        help="Override the run_after_import field in the JSON file. "
             "Use '--run' for True, '--run true' or '--run false' to set explicitly."
    )
    import_parser.set_defaults(func=lambda cmd_args: import_job(
        job_file=cmd_args.input_file,
        run_after_import=cmd_args.run_after_import
    ))

    jobs_parser = add_parser("jobs", "List jobs")
    jobs_parser.add_argument(
        "--output", "-o",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",
        default="__DEFAULT__",
        help="Path to save the job list (optional output path)"
    )
    jobs_parser.set_defaults(func=lambda cmd_args: list_jobs(output_path=cmd_args.output_path))

    observation_parser = add_parser("upload-obs", "Upload observational data CSV for a calibration job")
    observation_parser.add_argument("run_id", type=int, help="Calibration job ID")
    observation_parser.add_argument("csv_file", help="Path to the observational CSV file")
    observation_parser.set_defaults(func=lambda cmd_args: upload_observational_data(cmd_args.csv_file, cmd_args.run_id))

    register_parser = add_parser("register", "Register new user")
    register_parser.add_argument("email", nargs="?", help="Email address")
    register_parser.set_defaults(func=lambda cmd_args: ngen_register(cmd_args.email))

    regionalization_parser = add_parser("regionalization", "Generate files for regionalization")
    regionalization_parser.add_argument(
        "run_ids",
        type=int,
        nargs="*",  # <-- allow 0 or more positional run IDs
        help="One or more calibration job IDs"
    )

    # Optional file input
    regionalization_parser.add_argument(
        "--id-file",
        dest="id_file",
        help="Path to file containing calibration job IDs (comma, space, or newline separated)"
    )

    regionalization_parser.add_argument(
        "--output", "-o",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",  # Use the sentinel value
        default="__DEFAULT__",
        help="Path to save output files"
    )

    def _handle_regionalization_args(cmd_args):
        if cmd_args.id_file:
            run_ids_input = cmd_args.id_file
        elif cmd_args.run_ids:
            run_ids_input = cmd_args.run_ids
        else:
            print("Error: You must provide either run IDs as arguments or via --id-file.")
            return 1

        return generate_regionalization_files(run_ids_input, cmd_args.output_path)

    regionalization_parser.set_defaults(func=_handle_regionalization_args)

    run_parser = add_parser("run", "Submit calibration job")
    run_parser.add_argument("run_id", type=int, help="Calibration job ID")
    run_parser.set_defaults(func=lambda cmd_args: run_job(cmd_args.run_id))

    show_parser = add_parser("show", "Display job details")
    show_parser.add_argument("run_id", type=int, help="Calibration job ID")
    show_parser.add_argument(
        "--export", "-e",
        dest="output_path",
        nargs="?",
        const="__DEFAULT__",  # Use a sentinel value
        help="Export job details to a file (optional output path)"
    )
    show_parser.set_defaults(func=lambda cmd_args: handle_export_display(
        calibration_run_id=cmd_args.run_id,
        output_path=cmd_args.output_path,
        display=True
    ))

    status_parser = add_parser("status", "Show status of calibration job and related jobs")
    status_parser.add_argument("run_id", type=int, help="Calibration job ID")
    status_parser.set_defaults(func=lambda cmd_args: job_status(cmd_args.run_id))

    unarchive_parser = add_parser("unarchive", "Unarchive one or more jobs")
    unarchive_parser.add_argument(
        "run_ids",
        type=int,
        nargs="+",  # One or more space-separated integers
        help="One or more calibration job IDs"
    )
    unarchive_parser.set_defaults(func=lambda cmd_args: unarchive_job(cmd_args.run_ids))

    update_parser = add_parser("update", "Update job from a JSON file")
    update_parser.add_argument("run_id", type=int, help="Calibration job ID")
    update_parser.add_argument("input_file", help="Path to the JSON file")
    update_parser.add_argument(
        "--run", "-r",
        dest="run_after_update",
        nargs="?",
        type=str_to_bool,
        const=True,  # Default to True if specified without a value
        default=None,
        help="Override the run_after_import field in the JSON file. "
             "Use '--run' for True, '--run true' or '--run false' to set explicitly."
    )
    update_parser.set_defaults(func=lambda cmd_args: update_job(
        calibration_run_id=cmd_args.run_id,
        job_file=cmd_args.input_file,
        run_after_update=cmd_args.run_after_update
    ))

    # Parse arguments and execute the selected command
    args = parser.parse_args()

    # Authenticate if needed
    if args.command not in COMMANDS_AUTH_EXEMPT:
        try:
            if not ngen_login():
                print("Error logging in.  Use 'ngencerf register' to register a new userid")
                sys.exit(1)
        except Exception as e:
            print(f'Error communicating with server - {e}')
            sys.exit(1)

    # Call the appropriate handler and exit with the returned code
    if hasattr(args, "func"):
        sys.exit(args.func(args))
    else:
        print(f"No handler found for command: {args.command}")
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)
