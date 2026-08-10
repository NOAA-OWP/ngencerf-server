import csv
import logging
from pathlib import Path
from typing import cast

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from calibration.models import Gage, Rfc, CustomUser, Domain
from cerfServer.settings import BASE_DIR

logger = logging.getLogger(__name__)

# Build lookup dicts as case-insensitive (uppercased keys)
domain_dict = {
    d["name"].upper(): d["id"]
    for d in Domain.objects.only("id", "name").values("id", "name")
}

rfc_dict = {
    rfc["name"].upper(): rfc["id"]
    for rfc in Rfc.objects.only("id", "name").values("id", "name")
}

# In-memory staging area keyed by gage_id; each value is a dict of Gage fields suitable for update_or_create().
gages: dict[str, dict] = {}


class Command(BaseCommand):
    help = "Initialize Gage table"

    def add_arguments(self, parser):
        """
        Configure CLI arguments for the management command.

        --data_dir: Directory containing:
          - gages.csv (primary input)
          - inactive_gages.csv (optional overrides applied after gages.csv is read)
        """
        parser.add_argument("--data_dir", type=str, help="Path to directory containing gage files.")

    def handle(self, *args, **options):
        """
        Main entry point for the command.

        Workflow:
        1) Resolve input directory and admin user.
        2) Read all gage records from gages.csv into the in-memory `gages` dict.
        3) Apply inactive_gages.csv overrides (if present) by forcing is_active=False for listed gage_ids.
        4) Upsert records into the Gage table via update_or_create().

        Note:
        - This command currently matches records by gage_id only. If you ever expect multiple agencies
          per gage_id, you should change the lookup to use (gage_id, agency) to match the DB constraint.
        """
        try:
            data_dir = Path(options["data_dir"]) if options["data_dir"] else Path(BASE_DIR) / "calibration/management/commands/gage_data"
            logger.info(f"Reading data from {data_dir}")

            if not data_dir.is_dir():
                msg = f"{data_dir} must be a directory containing the data files."
                logger.error(msg)
                raise CommandError(msg)

            # Gage.objects.all().delete()

            try:
                # Need a user that is guaranteed to exist
                user = get_user_model().objects.get(email="admin@nextgenwaterprediction.com")
            except ObjectDoesNotExist:
                logger.error("********************************")
                logger.error("** Admin user does not exist. **")
                logger.error("********************************")
                raise CommandError("Admin user does not exist. Cannot proceed with gage initialization.")

            logger.info(f"In init_gages: email: {cast(CustomUser, user).email}")

            # Load primary file (all gage attributes)
            read_gages(data_dir / "gages.csv")

            # Apply inactive overrides last (optional)
            inactive_path = data_dir / "inactive_gages.csv"
            if inactive_path.exists():
                apply_inactive_overrides(inactive_path)
            else:
                logger.info(f"{inactive_path} not found; skipping inactive overrides")

            # Write to DB
            logger.info("")
            logger.info("Creating objects.... this will take a minute or two")
            row_num = 0

            # IMPORTANT:
            # The model has a uniqueness constraint on (gage_id, agency), but this loader currently upserts by gage_id only.
            # If the input data ever contains the same gage_id under multiple agencies, you must include agency in the lookup.
            unique_field = "gage_id"

            for gage in gages.values():
                gage["created_by"] = user
                try:
                    Gage.objects.update_or_create(
                        defaults={key: value for key, value in gage.items() if key != unique_field},
                        **{unique_field: gage[unique_field]}
                    )

                except Exception as e:
                    raise CommandError(f"Error adding gage - {gage} - {e}")

                row_num += 1
                if row_num % 1000 == 0:
                    logger.info(f"{row_num} of {len(gages)}...")

            logger.info("init_gages completed successfully.")

        except CommandError:
            # Already logged — ensures Django exits with non-zero code
            raise
        except Exception as e:
            logger.exception(f"Unexpected error during init_gages: {e}")
            raise CommandError(f"init_gages failed: {e}")


def apply_inactive_overrides(inactive_file: Path) -> None:
    """
    Read inactive_gages.csv and mark matching gages as inactive in the in-memory dict.

    File format:
      - One gage_id per line
      - Empty lines and lines starting with '#' are ignored

    Behavior:
      - If a gage_id exists in the staged `gages` dict, its 'is_active' field is set to False.
      - If a gage_id does not exist in `gages`, a warning is logged and the line is ignored.
    """
    inactive_count = 0
    with inactive_file.open() as file:
        for raw in file:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            gage_id = line
            if gage_id in gages:
                gages[gage_id]["is_active"] = False
                inactive_count += 1
            else:
                logger.warning(f"Could not find gage_id '{gage_id}' in loaded gages for deactivation")

    logger.info(f"Processed {inactive_count} inactive gages from {inactive_file.name}.")


def parse_bool(val: str | None) -> bool | None:
    """
    Parse a boolean value from CSV text.

    Returns:
      - True  for 'true'
      - False for 'false'
      - None  for '' or None

    Note:
      This function preserves missing values by returning None.
      Callers must replace None with an explicit default before
      assigning to model fields that are null=False.
    """
    if val is None:
        return None
    v = val.strip().lower()
    if v == "":
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    raise ValueError(f"Invalid boolean value '{val}' (expected 'true' or 'false')")


def parse_float(val: str | None) -> float | None:
    """
    Parse a float value from the CSV.

    Returns:
      - None when the input is '' or None (suitable for nullable DB fields)
      - float(...) otherwise

    Raises:
      - ValueError if the input is non-empty but not a valid float.
    """
    if val is None:
        return None
    v = val.strip()
    if v == "":
        return None
    return float(v)


def parse_str(val: str | None) -> str | None:
    """
    Parse a string value from the CSV.

    Returns:
      - None when the input is '' or None (suitable for nullable DB fields)
      - the stripped string otherwise
    """
    if val is None:
        return None
    v = val.strip()
    return v if v != "" else None


def require_str(row: dict[str, str], key: str, *, row_num: int, file_name: str, allow_empty: bool = False) -> str:
    """
    Read a required string column.

    - By default, the value must be non-empty after stripping.
    - If allow_empty=True, the column must exist, but may be an empty string.

    Use allow_empty=True for fields that are null=False but allow '' at the DB level
    (e.g., station_name).
    """
    if key not in row:
        raise CommandError(f"{file_name}: missing required column '{key}' on row {row_num}")

    raw = row.get(key)
    if raw is None:
        raise CommandError(f"{file_name}: missing required '{key}' on row {row_num}")

    value = raw.strip()

    if not value and not allow_empty:
        raise CommandError(f"{file_name}: missing required '{key}' on row {row_num}")

    return value  # may be '' if allow_empty=True


def domain_id_from_value(domain_value: str | None, *, row_num: int, file_name: str) -> int:
    """
    Convert CSV domain value -> Domain FK id (case-insensitive).

    The CSV contains a Domain "name" value; this function looks it up in `domain_dict`
    and returns the corresponding DB id.

    Domain is required (Gage.domain is null=False).
    """
    name = (domain_value or "").strip()
    if not name:
        raise CommandError(f"{file_name}: missing required 'domain' on row {row_num}")

    key = name.upper()
    try:
        return domain_dict[key]
    except KeyError:
        raise CommandError(f"{file_name}: unknown domain '{name}' on row {row_num}")


def rfc_id_from_value(rfc_value: str | None, *, row_num: int, file_name: str) -> int | None:
    """
    Convert CSV RFC value -> RFC FK id (case-insensitive).

    The CSV contains an RFC "name" value; this function looks it up in `rfc_dict`
    and returns the corresponding DB id.

    RFC is optional (Gage.rfc is null=True):
      - None / '' / whitespace -> None
      - otherwise must exist in rfc_dict
    """
    rfc_name = (rfc_value or "").strip()
    if not rfc_name:
        return None

    key = rfc_name.upper()
    try:
        return rfc_dict[key]
    except KeyError:
        raise CommandError(f"{file_name}: unknown rfc '{rfc_name}' on row {row_num}")


def read_gages(gages_file: Path) -> None:
    """
    Read gages.csv into the global `gages` dict.

    - Uses the CSV header via DictReader (no hard-coded columns / required list).
    - Validates required (null=False) Gage fields that do not have safe "blank" semantics:
        * gage_id
        * agency
        * station_name
        * huc
        * domain (resolved to domain_id)
    - Parses booleans and ensures they never become None before writing to null=False model fields:
        * is_active
        * headwater_calibration
        * nwm_v3_calibration
      (If blank in the CSV, the loader applies the model’s defaults explicitly.)
    - Allows blanks for nullable fields:
        * nws_id
        * rfc (resolved to rfc_id)
        * latitude, longitude, altitude
        * drainage_area
    """
    if not gages_file.exists():
        raise CommandError(f"{gages_file} does not exist")

    with gages_file.open(newline="") as file:
        reader = csv.DictReader(file, delimiter=",")

        gage_count = 0
        for row_num, row in enumerate(reader, start=2):  # header is row 1
            gage_count += 1

            gage_id = require_str(row, "gage_id", row_num=row_num, file_name=gages_file.name)
            agency = require_str(row, "agency", row_num=row_num, file_name=gages_file.name)
            station_name = require_str(row, "station_name", row_num=row_num, file_name=gages_file.name, allow_empty=True)
            huc = require_str(row, "huc", row_num=row_num, file_name=gages_file.name, allow_empty=True)

            domain_id = domain_id_from_value(row.get("domain"), row_num=row_num, file_name=gages_file.name)
            rfc_id = rfc_id_from_value(row.get("rfc"), row_num=row_num, file_name=gages_file.name)

            is_active = parse_bool(row.get("is_active"))
            headwater_calibration = parse_bool(row.get("headwater_calibration"))
            nwm_v3_calibration = parse_bool(row.get("nwm_v3_calibration"))

            # Ensure null=False booleans never get None (model has defaults; keep it explicit here)
            is_active = True if is_active is None else is_active
            headwater_calibration = False if headwater_calibration is None else headwater_calibration
            nwm_v3_calibration = False if nwm_v3_calibration is None else nwm_v3_calibration

            # Stage/update in global dict; later we upsert into DB.
            gage = gages.get(gage_id)
            if not gage:
                gage = {"gage_id": gage_id}
                gages[gage_id] = gage

            gage.update(
                {
                    "gage_id": gage_id,
                    "nws_id": parse_str(row.get("nws_id")),
                    "rfc_id": rfc_id,
                    "nwm_v3_calibration": nwm_v3_calibration,
                    "headwater_calibration": headwater_calibration,
                    "agency": agency,
                    "station_name": station_name,
                    "latitude": parse_float(row.get("latitude")),
                    "longitude": parse_float(row.get("longitude")),
                    "altitude": parse_float(row.get("altitude")),
                    "huc": huc,
                    "drainage_area": parse_float(row.get("drainage_area")),
                    "domain_id": domain_id,
                    "is_active": is_active,
                }
            )

    logger.info(f"Processed {gage_count} gages from {gages_file.name}.")
