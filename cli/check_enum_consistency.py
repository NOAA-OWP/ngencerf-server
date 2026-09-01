#!/usr/bin/env python3
import os
import sys

# ----------------------------------------------------------------------
# Allow this script to import from the main cerfServer project when
# executed inside the CLI directory (e.g., /ngencerf/ngencerf-server/cli).
#
# We assume this script is located at:
#     /ngencerf/ngencerf-server/cli/check_enum_consistency.py
#
# So the project root (where "calibration" lives) is one level up.
# ----------------------------------------------------------------------
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(str(__file__)), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Verify that the calibration package is visible
if not os.path.exists(os.path.join(PROJECT_ROOT, "calibration")):
    print(f"Calibration package not found at {os.path.join(PROJECT_ROOT, 'calibration')}")
    sys.exit(0)

try:
    from calibration.enums_vanilla import CalibrationSortField as ServerEnum
except ModuleNotFoundError as e:
    print(f"Server package not found or not importable: {e}")
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
