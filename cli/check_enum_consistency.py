#!/usr/bin/env python3
import os
import sys

# ----------------------------------------------------------------------
# Allow CLI to import from the main cerfServer project when running locally
# ----------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from calibration.enums_vanilla import CalibrationSortField as ServerEnum
except ModuleNotFoundError:
    print("Server package not found. Skipping enum consistency check.")
    sys.exit(0)

try:
    from ngencerf.calibration_sort_fields import CalibrationSortField as CliEnum
except ModuleNotFoundError:
    print("Could not import CLI CalibrationSortField enum.")
    sys.exit(1)

server_fields = set(ServerEnum.get_names())
cli_fields = set(CliEnum.get_names())

if server_fields != cli_fields:
    print("Enum mismatch detected!")
    missing_in_cli = server_fields - cli_fields
    missing_in_server = cli_fields - server_fields
    if missing_in_cli:
        print(f"  Missing in CLI: {sorted(missing_in_cli)}")
    if missing_in_server:
        print(f"  Missing in Server: {sorted(missing_in_server)}")
    sys.exit(1)

print("CalibrationSortField enums are consistent.")
