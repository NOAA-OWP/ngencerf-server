import csv
import logging
from pathlib import Path
from typing import cast

from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand, CommandError

from calibration.enums import DomainEnum
from calibration.models import Gage, Rfc, CustomUser
from cerfServer.settings import BASE_DIR

logger = logging.getLogger(__name__)

# Gages are loaded from several files
# 1 USGS files
# 2 NWMv3 files.  These are the NWM calibratable gages
# 3 Mapping files to get nws_id
# 4 Additional gages

alaska_domain = DomainEnum.get_instance('Alaska')
hawaii_domain = DomainEnum.get_instance('Hawaii')
puerto_rico_domain = DomainEnum.get_instance('Puerto_Rico')
conus_domain = DomainEnum.get_instance('CONUS')

rfc_dict = {rfc['name']: rfc['id'] for rfc in list(Rfc.objects.only('id', 'name').values('id', 'name'))}

gages = {}


class Command(BaseCommand):
    help = "Initialize Gage table"

    def add_arguments(self, parser):
        parser.add_argument('--data_dir', type=str, help='Path to directory containing gage files.')

    def handle(self, *args, **options):
        """Main entry point — wrapped with try/except to raise CommandError on any failure."""
        try:
            data_dir = Path(options['data_dir']) if options['data_dir'] else Path(BASE_DIR) / 'calibration/management/commands/gage_data'
            logger.info(f'Reading data from {data_dir}')

            if not data_dir.is_dir():
                msg = f"{data_dir} must be a directory containing the data files."
                logger.error(msg)
                raise CommandError(msg)

            # Gage.objects.all().delete()

            try:
                # need to get a user that is guaranteed to be there, such as admin
                user = get_user_model().objects.get(email='admin@nextgenwaterprediction.com')
            except ObjectDoesNotExist:
                logger.error('********************************')
                logger.error('** Admin user does not exist. **')
                logger.error('********************************')
                msg = "Admin user does not exist. Cannot proceed with gage initialization."
                raise CommandError(msg)

            logger.info(f"In init_gages: email: {cast(CustomUser, user).email}")

            # -----------------------------------------------------------
            # Run all data-loading steps. Any exception bubbles to outer handler.
            # -----------------------------------------------------------
            add_usgs_gages(data_dir / 'USGS_gages_CONUS.csv', conus_domain)
            add_usgs_gages(data_dir / 'USGS_gages_AK.csv', alaska_domain)
            add_usgs_gages(data_dir / 'USGS_gages_HI.csv', hawaii_domain)
            add_usgs_gages(data_dir / 'USGS_gages_PR.csv', puerto_rico_domain)

            add_nwm_v3(data_dir / 'NWMv3_calibration_basins_CONUS.csv', conus_domain)
            add_nwm_v3(data_dir / 'NWMv3_calibration_basins_AK.csv', alaska_domain)
            add_nwm_v3(data_dir / 'NWMv3_calibration_basins_HI.csv', hawaii_domain)
            add_nwm_v3(data_dir / 'NWMv3_calibration_basins_PR.csv', puerto_rico_domain)

            # Some extra manually added gages
            with (data_dir / 'Supplemental - AK.csv').open() as file:
                # Skip the first 2 lines before header
                for _ in range(2):
                    next(file)
                reader = csv.DictReader(file, delimiter=',')
                gage_count = 0
                row: dict[str, str]
                for row in reader:
                    gage_count += 1
                    gage_id = row.get('gage_id')
                    # These are all new gages
                    gage = {
                        'gage_id': gage_id,
                        'nws_id': row.get('nws_id'),
                        'longitude': row.get('long'),
                        'latitude': row.get('lat'),
                        'station_name': row.get('station_name'),
                        'is_active': True,
                        'nwm_v3_calibration': False,
                        'headwater_calibration': True,
                        'domain_id': alaska_domain.id
                    }
                    gages[gage_id] = gage
            logger.info(f'Processed {gage_count} gages from {file.name}.')

            with (data_dir / 'Supplemental - CONUS.csv').open() as file:
                # Skip the first line before header
                for _ in range(1):
                    next(file)
                reader = csv.DictReader(file, delimiter='|')
                new_count = 0
                existing_count = 0
                gage_count = 0
                row: dict[str, str]
                for row in reader:
                    gage_count += 1
                    gage_id = row.get('gage_id')
                    gage = gages.get(gage_id)
                    # These gages should already exist, so we'll check for that.
                    # We'll create it, just in case it doesn't
                    if not gage:
                        new_count += 1
                        gage = {'gage_id': gage_id, 'is_active': True, 'domain_id': conus_domain.id}
                        gages[gage_id] = gage
                    else:
                        existing_count += 1

                    # Use new value only if old value doesn't exist
                    nws_id = row.get('nws_id').strip() or gage.get('nws_id')
                    station_name = row.get('station_name') or gage.get('station_name')
                    agency = row.get('agency') or gage.get('agency')
                    rfc = row.get('rfc')
                    rfc_id = rfc_dict[rfc.strip()] if rfc else None

                    gage.update({
                        'nws_id': nws_id or None,
                        'station_name': (station_name or '').strip(),
                        'rfc_id': rfc_id,
                        'headwater_calibration': True,
                        'agency': (agency or '').strip()
                    })

                    gages[gage_id] = gage
            logger.info(f'Processed {gage_count} gages from {file.name}.  {new_count} were new.  {existing_count} existing.')

            # This file maps NWS id with USGS id
            with (data_dir / 'ALL_USGS-HADS_SITES.txt').open() as file:
                # Skip the first 4 lines
                for _ in range(4):
                    next(file)
                reader = csv.DictReader(file, delimiter='|', fieldnames=['nws_id', 'gage_id', 'goes_id', 'nws_hsa', 'latitude', 'longitude', 'station_name'])
                gage_count = 0
                skip_count = 0
                row: dict[str, str]
                for row in reader:
                    nws_id = row.get('nws_id', '').strip()
                    gage_id = row.get('gage_id', '').strip()
                    gage = gages.get(gage_id)
                    if not gage:
                        # logger.info(f"Can't find gage_id '{gage_id}' referenced in ALL_USGS-HADS_SITES.txt")
                        # According to Yuqiong, there are reservoir gage and not streamflow gages, so we can ignore them
                        skip_count += 1
                        continue
                    gage_count += 1

                    if 'latitude' not in gage or gage['latitude'] is None:
                        logger.info(f'Adding lat/long for gage {gage_id}')
                        # The ALL_USGS-HADS_SITES.txt file has all longitude values as positive, even though they are in the Western hemisphere.  So we'll switch it.
                        gage['latitude'] = dms_to_dd(row.get('latitude').strip())
                        gage['longitude'] = dms_to_dd('-' + row.get('longitude').strip())
                    if 'station_name' not in gage or gage['station_name'] is None:
                        gage['station_name'] = row.get('station_name')

                    gage['nws_id'] = nws_id
            logger.info(f'Processed {gage_count} gages from {file.name}.  Skipped {skip_count} gages assumed to be non-streamflow gages.')

            add_additional_gages(data_dir / 'RFC Additional NextGen Calibration Basin List - AK.csv', alaska_domain)
            add_additional_gages(data_dir / 'RFC Additional NextGen Calibration Basin List - CONUS.csv', conus_domain)
            add_additional_gages(data_dir / 'RFC Additional NextGen Calibration Basin List - PR.csv', puerto_rico_domain)
            add_additional_gages(data_dir / 'RFC Additional NextGen Calibration Basin List - HI.csv', hawaii_domain)

            # Deactivate gages.  This one should be done last
            with (data_dir / 'inactive_gages.csv').open() as file:
                inactive_count = 0
                for raw in file:
                    line = raw.strip()
                    # Skip empty lines or comments
                    if not line or line.startswith('#'):
                        continue

                    gage_id = line
                    if gage_id in gages:
                        gages[gage_id]['is_active'] = False
                        inactive_count += 1
                    else:
                        logger.warning(f"Could not find gage_id '{gage_id}' in loaded gages for deactivation")

                logger.info(f'Processed {inactive_count} inactive gages from {file.name}.')

            logger.info('')
            logger.info('Creating objects.... this will take a minute or two')
            row_num = 0
            unique_field = 'gage_id'
            for gage in gages.values():
                gage['created_by'] = user
                try:
                    Gage.objects.update_or_create(
                        defaults={key: value for key, value in gage.items() if key != unique_field},
                        **{unique_field: gage[unique_field]}
                    )

                except Exception as e:
                    raise CommandError(f'Error adding gage - {gage} - {e}')
                row_num += 1
                if row_num % 1000 == 0:
                    logger.info(f'{row_num} of {len(gages)}...')

            logger.info("init_gages completed successfully.")

        except CommandError:
            # Already logged — ensures Django exits with non-zero code
            raise
        except Exception as e:
            logger.exception(f"Unexpected error during init_gages: {e}")
            raise CommandError(f"init_gages failed: {e}")


def add_additional_gages(gage_file, domain):
    with Path(gage_file).open() as file:
        reader = csv.reader(file, delimiter=',')
        row_num = 0
        gage_count = 0
        row: list[str]
        for row in reader:
            row_num += 1
            # Skip the first lines
            if row_num <= 1:
                continue

            rfc = row[0]
            rfc_id = rfc_dict[rfc]
            for nws_id in row[1:]:
                # Find this nws_id in our collection
                gage = next((item for item in gages.values() if item.get('nws_id') == nws_id), None)
                if not gage:
                    logger.info(f"Could not find gage with nws_id {nws_id} for rfc {rfc}")
                    continue
                gage_count += 1
                gage['rfc_id'] = rfc_id
                gage['domain_id'] = domain.id
                # All of these gages have headwater_calibration flag on regardless of nwm_v3_calibration
                gage['headwater_calibration'] = True
    logger.info(f'Processed {gage_count} gages from {file.name}.')


def add_usgs_gages(usgs_file, domain):
    # Read the main file and supplement with info from the previous file, if available for that gage
    with Path(usgs_file).open() as file:
        # Skip the first 34 lines, including the header
        for _ in range(34):
            next(file)
        reader = csv.DictReader(file, delimiter='\t',
                                fieldnames=['agency_name', 'gage_id', 'station_name', 'site_type', 'latitude', 'longitude', 'lat_long_accuracy',
                                            'lat_Long_datum', 'altitude', 'altitude_accuracy', 'altitude_datum', 'huc', 'drainage_area'])

        gage_count = 0
        row: dict[str, str]
        for row in reader:
            gage_count += 1
            gage_id = row.get('gage_id')
            # There shouldn't be any overlap in the USGS files, so we should always be creating a new entry.
            gage = gages.get(gage_id)
            if not gage:
                gage = {'gage_id': gage_id, 'is_active': True, 'nwm_v3_calibration': False, 'headwater_calibration': False}
                gages[gage_id] = gage

            agency = row.get('agency_name')
            station_name = row.get('station_name')
            site_type = row.get('site_type')
            latitude = float(row.get('latitude'))
            longitude = float(row.get('longitude'))
            lat_long_accuracy = row.get('lat_long_accuracy', '')
            lat_long_datum = row.get('lat_long_datum', '')
            altitude = float(row.get('altitude')) if row.get('altitude') else None
            altitude_accuracy = row.get('altitude_accuracy', '')
            altitude_datum = row.get('altitude_datum', '')
            huc = row.get('huc')
            drainage_area = float(row.get('drainage_area')) if row.get('drainage_area') else None

            gage.update({'agency': agency, 'station_name': station_name, 'site_type': site_type,
                         'lat_long_accuracy': lat_long_accuracy, 'lat_long_datum': lat_long_datum,
                         'altitude': altitude, 'altitude_accuracy': altitude_accuracy, 'altitude_datum': altitude_datum, 'huc': huc,
                         'drainage_area': drainage_area, 'latitude': latitude, 'longitude': longitude, 'domain_id': domain.id})

    logger.info(f'Processed {gage_count} gages from {file.name}.')


def add_nwm_v3(nwm_v3_file, domain):
    with Path(nwm_v3_file).open() as file:
        reader = csv.DictReader(file, delimiter=',')
        new_count = 0
        existing_count = 0
        gage_count = 0
        row: dict[str, str]
        for row in reader:
            gage_count += 1
            gage_id = row.get('ID')
            gage = gages.get(gage_id)
            if not gage:
                new_count += 1
                longitude = None if row.get('longitd') == 'NA' else float(row.get('longitd'))
                latitude = None if row.get('latitud') == 'NA' else float(row.get('latitud'))
                gage = {'gage_id': gage_id, 'is_active': True,
                        'nwm_v3_calibration': True, 'headwater_calibration': True,
                        'latitude': latitude, 'longitude': longitude,
                        'domain_id': domain.id}
                gages[gage_id] = gage
            else:
                # If it already exists, update this flag
                existing_count += 1
                gage['nwm_v3_calibration'] = True
                gage['headwater_calibration'] = True

            rfc = row.get('rfc')
            gage['rfc_id'] = rfc_dict[rfc] if rfc else None
            gages[gage_id] = gage
    logger.info(f'Processed {gage_count} gages from {file.name}.  {new_count} were new.  {existing_count} existing')


def dms_to_dd(lat_long_str):
    d, m, s = tuple(lat_long_str.split(' '))
    if d[0] == '-':
        dd = float(d) - float(m) / 60 - float(s) / 3600
    else:
        dd = float(d) + float(m) / 60 + float(s) / 3600

    return dd

#
# bounding_boxes = [{'name': 'Alaska', 'upper_right': {'lat': 51.229087747767466, 'long': -157.68842},
#                    'lower_left': {'lat': 71.352561, 'long': -139.55319}},
#                   {'name': 'Hawaii', 'upper_right': {'lat': 18.91727560534605, 'long': -160.33116},
#                    'lower_left': {'lat': 22.23238695135951, 'long': -154.80833743387433}},
#                   {'name': 'Puerto Rico', 'upper_right': {'lat': 17.91217576734767, 'long': -67.33337},
#                    'lower_left': {'lat': 18.51609472983729, 'long': -64.48663}},
#                   ]
#
# # Normalize the longitude, so we don't have to worry about negatives
# for b in bounding_boxes:
#     b['upper_right']['long'] += 180
#     b['lower_left']['long'] += 180

#
# def calculate_domain(lat, long):
#     lat = float(lat)
#     long = float(long) + 180.0
#     # Oder matters.  Do the unambiguous ones first
#     puerto_rico = next(item for item in bounding_boxes if item['name'] == 'Puerto Rico')
#     if (puerto_rico['lower_left']['lat'] < lat < puerto_rico['upper_right']['lat']
#             and puerto_rico['lower_left']['long'] < lat < puerto_rico['upper_right']['long']):
#         return puerto_rico_domain
#
#     hawaii = next(item for item in bounding_boxes if item['name'] == 'Hawaii')
#     if (hawaii['lower_left']['lat'] < lat < hawaii['upper_right']['lat']
#             and hawaii['lower_left']['long'] < lat < hawaii['upper_right']['long']):
#         return hawaii_domain
#
#     alaska = next(item for item in bounding_boxes if item['name'] == 'Alaska')
#     # For alaska, where just going to check if the lat/long is West of the Eastern border
#     if long < alaska['upper_right']['long']:
#         return alaska_domain
#
#     # Assume anything else is Conus
#     return conus_domain
