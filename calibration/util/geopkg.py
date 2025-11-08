import logging
import os
import sqlite3
import traceback
from contextlib import contextmanager
from functools import lru_cache
from io import BytesIO
from itertools import cycle
from pathlib import Path

import fiona
import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt

from calibration.util.cloud_util import localize_to_path

logger = logging.getLogger(__name__)
logging.getLogger("pyogrio._io").setLevel(logging.WARNING)
if not logging.getLogger().hasHandlers():
    # We're likely running outside Django — configure basic logging to stderr
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

# See https://stackoverflow.com/questions/27147300/matplotlib-tcl-asyncdelete-async-handler-deleted-by-the-wrong-thread
matplotlib.use('Agg')  # Use a backend that doesn't require a display (like for generating images)

layer_style_config = {
    'nexus': {
        'markersize': 100,
        'color': 'blue',
        'plot_method': 'point'
    },
    'flowpaths': {
        'linewidth': 2.0,
        'linestyle': '--',  # Use ':' for dotted, '--' for dashed
        'color': 'green',
        'plot_method': 'line'
    },
    'flowlines': {
        'linewidth': 2.0,
        'color': 'red',
        'plot_method': 'line'
    },
    'divides': {
        'linewidth': 2.0,
        'color': 'black',
        'plot_method': 'boundary'  # purely informational
    }
}


def _pp(orig: str, local: str) -> str:
    """
    Pretty-print a path pair:
    - If same: return "'orig'"
    - If different: return "original 'orig' (local copy: local)"
    """
    try:
        same = os.path.abspath(str(orig)) == os.path.abspath(str(local))
    except Exception:
        same = (orig == local)
    return f"original '{orig}' (local copy: {local})" if not same else f"'{orig}'"


@contextmanager
def _localize_gpkg(gpkg_path: str):
    # Always use persistent cache for GeoPackages. Infer suffix for robustness.
    ext = os.path.splitext(str(gpkg_path))[1] or ".gpkg"
    with localize_to_path(gpkg_path, enable_cache=True, suffix=ext) as (orig, local):
        yield orig, local


# ----------------------------
# Public helpers that may be called from other modules
# ----------------------------

def check_file_accessible(file_path: str) -> None:
    """
    Checks if a file exists, is a valid file (not a directory), and is readable.

    :param file_path: Path to the file.
    :raises FileNotFoundError: If the file does not exist.
    :raises IsADirectoryError: If the file path is a directory instead of a file.
    :raises PermissionError: If the file exists but is not readable.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"GeoPackage file not found: {file_path}")
    if not os.path.isfile(file_path):
        raise IsADirectoryError(f"Expected a file but found a directory: {file_path}")
    if not os.access(file_path, os.R_OK):
        raise PermissionError(f"Permission denied: {file_path}")


def safe_read_gpkg(gpkg_path: str, layer: str = None) -> gpd.GeoDataFrame:
    """
    Read a specific layer from a GeoPackage file with detailed error handling.

    This function wraps GeoPandas' `read_file()` to provide informative diagnostics
    when file reading fails due to reasons such as:
      - Missing file or layer
      - Corrupted or invalid GeoPackage
      - Missing required drivers
      - Invalid geometry or projection data

    :param gpkg_path: Path to the GeoPackage (.gpkg) file.
    :param layer: Optional name of the layer to read. If None, the default layer is loaded.
    :return: A GeoDataFrame containing the requested layer's features and attributes.
    :raises RuntimeError: If the file cannot be opened or parsed, with context such as:
                          - Whether the file exists
                          - File size (if available)
                          - List of available layers (if accessible)
                          - Underlying exception details
    """
    try:
        return gpd.read_file(gpkg_path, layer=layer)
    except fiona.errors.DriverError as e:
        raise RuntimeError(f"Could not open {gpkg_path}. Ensure it is a valid GeoPackage. Error: {e}")
    except Exception as e:
        raise RuntimeError(
            f"Failed to read '{layer}' layer from {gpkg_path}. Possible issues:\n"
            f"  - File exists: {os.path.exists(gpkg_path)}\n"
            f"  - File size: {os.path.getsize(gpkg_path) if os.path.exists(gpkg_path) else 'N/A'} bytes\n"
            f"  - Available layers: {list_layers(gpkg_path) if os.path.exists(gpkg_path) else 'N/A'}\n"
            f"Error details: {e}"
        )


@lru_cache()
def gpkg_to_png_selected_layers(gpkg_path: str, layers_to_include: tuple[str, ...] = None) -> BytesIO:
    """
    Generates a PNG image from selected layers in a GeoPackage and returns it as a BytesIO object.
    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param layers_to_include: Tuple of layer names to include in the plot. Defaults to a predefined set.
    :return: BytesIO object containing the generated PNG image.
    :raises FileNotFoundError: If the GeoPackage file does not exist (after localization).
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            check_file_accessible(local_path)
        except Exception as e:
            raise RuntimeError(f"GeoPackage not accessible at {_pp(orig_path, local_path)}. Error: {e}")

        if layers_to_include is None:
            layers_to_include = ('nexus', 'flowpaths', 'flowlines')  # Default layers to include

        # Initialize the plot
        fig, ax = plt.subplots(figsize=(15, 13), dpi=300)  # Larger figure and higher resolution

        available_layers = list_layers(local_path)

        # Plot the divides layer (outline) if it exists
        if 'divides' in available_layers:
            try:
                divides_gdf = safe_read_gpkg(local_path, layer='divides')
                # Simplify geometries for performance improvement
                divides_gdf['geometry'] = divides_gdf['geometry'].simplify(tolerance=0.01, preserve_topology=True)

                # Create boundary GeoDataFrame (Shapely 2.x compatible)
                boundary_gdf = gpd.GeoDataFrame(geometry=divides_gdf.geometry.boundary, crs=divides_gdf.crs)
                style = layer_style_config.get('divides', {})
                color = style.get('color', 'black')
                linewidth = style.get('linewidth', 1.5)

                boundary_gdf.plot(ax=ax, color=color, linewidth=linewidth)
            except Exception as e:
                raise RuntimeError(f"Failed to read or plot 'divides' layer from {_pp(orig_path, local_path)}. Error: {e}")

        # Color cycle fallback for layers not in config
        color_cycle = cycle(['blue', 'green', 'red', 'cyan', 'magenta'])

        # Plot each requested layer if it exists
        for layer in layers_to_include:
            if layer in available_layers:
                try:
                    layer_gdf = safe_read_gpkg(local_path, layer=layer)
                    layer_gdf['geometry'] = layer_gdf['geometry'].simplify(tolerance=0.01, preserve_topology=True)

                    style = layer_style_config.get(layer, {})
                    if not style:
                        logger.warning(f"No style config for '{layer}'. Using fallback (source: {_pp(orig_path, local_path)}).")

                    plot_method = style.get('plot_method', 'line')
                    color = style.get('color', next(color_cycle))
                    linewidth = style.get('linewidth', 1.5)
                    linestyle = style.get('linestyle', '-')
                    markersize = style.get('markersize', 10)

                    if plot_method == 'point':
                        layer_gdf.plot(ax=ax, color=color, markersize=markersize)
                    else:
                        layer_gdf.plot(ax=ax, color=color, linewidth=linewidth, linestyle=linestyle)

                except Exception as e:
                    raise RuntimeError(f"Failed to read or plot layer '{layer}' from {_pp(orig_path, local_path)}. Error: {e}")

            # Remove axes for better visualization
        ax.set_axis_off()

        # Convert the plot to an in-memory PNG file
        img_buffer = BytesIO()
        plt.savefig(
            img_buffer,
            format='png',
            bbox_inches='tight',
            pad_inches=0.05
        )

        plt.close()
        img_buffer.seek(0)
        return img_buffer


@lru_cache()
def get_geometry_from_gpkg(gpkg_path: str, catchment_layer: str = None, gage_layer: str = None) -> dict:
    """
    Extracts both catchment boundaries (as WKT) and gage coordinates (latitude & longitude) from a GeoPackage.

    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param catchment_layer: Name of the layer containing catchment boundaries. Defaults to 'divides'.
    :param gage_layer: Name of the layer containing gage locations. Defaults to 'hydrolocations'.
    :return: Dictionary containing:
             - "catchments": Mapping of catchment identifiers to their boundaries as WKT strings.
             - "gage_coordinates": Dictionary with 'latitude' and 'longitude'.
             - "crs": The coordinate reference system of the layers.
    :raises RuntimeError: If reading fails.
    :raises ValueError: If the specified layers or required columns are missing.
    """
    if catchment_layer is None:
        catchment_layer = "divides"
    if gage_layer is None:
        gage_layer = "hydrolocations"

    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            check_file_accessible(local_path)
        except Exception as e:
            raise RuntimeError(f"GeoPackage not accessible at {_pp(orig_path, local_path)}. Error: {e}")

        # List all layers to verify the requested layers exist
        available_layers = list_layers(local_path)

        if catchment_layer not in available_layers:
            raise ValueError(f"Catchment layer '{catchment_layer}' not found in {_pp(orig_path, local_path)}. "
                             f"Available layers: {available_layers}")

        if gage_layer not in available_layers:
            raise ValueError(f"Gage layer '{gage_layer}' not found in {_pp(orig_path, local_path)}. "
                             f"Available layers: {available_layers}")

        # Read the catchments layer
        try:
            gdf_catchments = safe_read_gpkg(local_path, layer=catchment_layer)
        except Exception as e:
            raise RuntimeError(f"Failed to read layer '{catchment_layer}' from {_pp(orig_path, local_path)}. Error: {e}")

        # Extract catchments, converting geometry to WKT
        if 'divide_id' not in gdf_catchments.columns or 'geometry' not in gdf_catchments.columns:
            raise ValueError("The required columns ('divide_id', 'geometry') were not found "
                             f"in the catchment layer for {_pp(orig_path, local_path)}.")

        catchments = {
            row['divide_id']: row['geometry'].wkt  # Convert to WKT (string)
            for _, row in gdf_catchments.iterrows()
        }

        # Read the gage layer
        try:
            gdf_gage = safe_read_gpkg(local_path, layer=gage_layer)
        except Exception as e:
            raise RuntimeError(f"Failed to read layer '{gage_layer}' from {_pp(orig_path, local_path)}. Error: {e}")

        # Ensure gage data exists and convert coordinates
        if gdf_gage.empty or 'hl_x' not in gdf_gage.columns or 'hl_y' not in gdf_gage.columns:
            gage_coordinates = None  # No valid gage data found
        else:
            # Convert hl_x, hl_y into a GeoDataFrame
            gdf_gage = gdf_gage.set_geometry(gpd.points_from_xy(gdf_gage.hl_x, gdf_gage.hl_y))

            # Assign CRS from Catchments if Gage CRS is missing
            if gdf_gage.crs is None:
                if gdf_catchments.crs:
                    gdf_gage.set_crs(gdf_catchments.crs, inplace=True)
                else:
                    raise RuntimeError("CRS is missing for both the gage and catchments layers in "
                                       f"{_pp(orig_path, local_path)}. Cannot convert to latitude/longitude.")

            # Convert to EPSG:4326 (WGS84 lat/lon)
            gdf_gage = gdf_gage.to_crs(epsg=4326)
            gage_point = gdf_gage.geometry.iloc[0]  # Assuming first entry is the gage
            gage_coordinates = {"latitude": gage_point.y, "longitude": gage_point.x}

        return {
            "catchments": catchments,
            "gage_coordinates": gage_coordinates,
            "crs": gdf_catchments.crs.to_string() if gdf_catchments.crs else None
        }


def normalize_gpkg(gpkg_path: str, output_path: str, *, output_is_dir: bool = False):
    """
    Normalize a GeoPackage file.

    This function:
    - Reprojects all spatial layers to EPSG:4326 (WGS84) unless they are already in EPSG:5070
    - Copies all non-spatial tables as-is using raw SQLite operations
    - Overwrites the output file if it already exists
    - If output_path is a directory (explicitly or by detection), saves the output using the same filename as gpkg_path
    - If output_path is a file and does not end with '.gpkg', appends the extension

    Accepts a local path or a remote URL for gpkg_path (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the source GeoPackage file.
    :param output_path: Path to the output directory or output file.
    :param output_is_dir: If True, force output_path to be interpreted as a directory, even if it does not exist.
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        input_filename = os.path.basename(local_path)

        # Determine final output file path
        if output_is_dir or (os.path.exists(output_path) and os.path.isdir(output_path)):
            if not os.path.exists(output_path):
                os.makedirs(output_path, exist_ok=True)
            output_path = os.path.join(output_path, input_filename)
        elif not output_path.lower().endswith(".gpkg"):
            logger.warning(f"Output path '{output_path}' does not end with '.gpkg'. Appending '.gpkg'.")
            output_path += ".gpkg"

        logger.info(f"Normalizing {_pp(orig_path, local_path)} to '{output_path}'")
        if os.path.exists(output_path):
            logger.info(f"Overwriting existing file: {output_path}")
            os.remove(output_path)

        # --- Show layers before normalization ---
        try:
            before_layers = list_layers(local_path)
            logger.info(f"Layers before normalization ({len(before_layers)}): {before_layers}")
        except Exception as e:
            logger.warning(f"Could not list layers before normalization for {_pp(orig_path, local_path)}: {e}")
            before_layers = []

        spatial_layers = []
        non_spatial_layers = []

        # First pass: identify spatial vs non-spatial and write spatial layers
        with fiona.Env():  # ensures GDAL handles close at block exit
            for layer_name in before_layers:
                try:
                    gdf = safe_read_gpkg(local_path, layer=layer_name)
                except Exception as e:
                    logger.error(f"Could not read layer '{layer_name}' from {_pp(orig_path, local_path)}. Error: {e}")
                    traceback.print_exc()
                    continue

                # Detect whether it's non-spatial
                if not isinstance(gdf, gpd.GeoDataFrame) or gdf.geometry.name not in gdf.columns:
                    non_spatial_layers.append(layer_name)
                    continue

                # Reproject or copy as-is
                if gdf.crs is None:
                    logger.warning(f"Layer '{layer_name}' has no CRS in {_pp(orig_path, local_path)}. Saving as-is.")
                    gdf_out = gdf
                elif gdf.crs.to_epsg() == 5070:
                    logger.info(f"Layer '{layer_name}' is already EPSG:5070 in {_pp(orig_path, local_path)}. Saving as-is.")
                    gdf_out = gdf
                else:
                    logger.info(f"Reprojecting layer '{layer_name}' from EPSG:{gdf.crs.to_epsg()} to EPSG:4326 "
                                f"for {_pp(orig_path, local_path)}.")
                    gdf_out = gdf.to_crs(epsg=4326)

                gdf_out.to_file(Path(output_path), layer=layer_name, driver="GPKG")

                spatial_layers.append(layer_name)

        # Second pass: copy non-spatial tables using SQLite
        with sqlite3.connect(local_path) as src_conn, sqlite3.connect(output_path) as dst_conn:
            for table in non_spatial_layers:
                logger.info(f"Copying non-spatial table '{table}' from {_pp(orig_path, local_path)}")
                try:
                    copy_non_spatial_table_one(table, src_conn, dst_conn)
                except Exception as e:
                    logger.error(f"Failed to copy non-spatial table '{table}' from {_pp(orig_path, local_path)}. Error: {e}")
                    traceback.print_exc()

        # --- Show layers after normalization ---
        try:
            after_layers = list_layers(output_path)
            logger.info(f"Layers after normalization ({len(after_layers)}): {after_layers}")
        except Exception as e:
            logger.warning(f"Could not list layers after normalization for '{output_path}': {e}")

        logger.info(f"Normalized GeoPackage written to: {output_path}")


def copy_non_spatial_table_one(table_name: str, src_conn: sqlite3.Connection, dst_conn: sqlite3.Connection):
    """
    Copy a single non-spatial table from src to dst SQLite connections.
    """
    src_cursor = src_conn.cursor()
    dst_cursor = dst_conn.cursor()

    # Create table
    # noinspection SqlResolve
    src_cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?;", (table_name,))
    create_stmt = src_cursor.fetchone()
    if not create_stmt:
        logger.warning(f"Table '{table_name}' not found in source.")

        return

    dst_cursor.execute(create_stmt[0])

    # Copy rows
    rows = src_cursor.execute(f'SELECT * FROM "{table_name}";').fetchall()
    if rows:
        placeholders = ", ".join(["?"] * len(rows[0]))
        dst_cursor.executemany(f"INSERT INTO '{table_name}' VALUES ({placeholders});", rows)

    dst_conn.commit()


def list_layers(gpkg_path: str) -> list[str]:
    """
    Retrieve all layer names from a GeoPackage file.

    Accepts a local path or a remote URL. Callers in this module typically pass
    the localized (cached) path returned by `_localize_gpkg`.

    :param gpkg_path: Path/URL to the GeoPackage (.gpkg) file.
    :return: A list of layer names available in the file.
    :raises RuntimeError: If the file cannot be opened or read as a GeoPackage.
    """
    # Keep this function simple: it can accept either a localized path already,
    # or the original path/URL. If it's a URL, the caller should have localized it.
    try:
        return fiona.listlayers(gpkg_path)
    except fiona.errors.DriverError as e:
        raise RuntimeError(f"Could not open GeoPackage '{gpkg_path}'. It may be corrupted or not a valid file. Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error listing layers in '{gpkg_path}': {e}")


def find_gage_id(gpkg_path: str, layer_name: str = "hydrolocations", field_name: str = "hl_uri") -> list[str]:
    """
    Extract unique gage IDs from a specified layer and field in the GeoPackage.

    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param layer_name: Layer expected to contain gage IDs (default is 'hydrolocations').
    :param field_name: Field in the layer that contains gage IDs (default is 'hl_uri').
    :return: List of unique gage ID strings, or an empty list if not found.
    :raises RuntimeError: If the layer cannot be read.
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            layers = list_layers(local_path)
            if layer_name not in layers:
                logger.info(f"Layer '{layer_name}' not found in {_pp(orig_path, local_path)}.")
                return []

            gdf = gpd.read_file(local_path, layer=layer_name)
            if field_name in gdf.columns:
                gage_ids = gdf[field_name].astype(str).unique().tolist()
                logger.info(f"Found gage_id(s) in layer '{layer_name}' from {_pp(orig_path, local_path)}: {gage_ids}")
                return gage_ids
            else:
                logger.info(f"Field '{field_name}' not found in layer '{layer_name}' for {_pp(orig_path, local_path)}.")
                return []
        except Exception as e:
            raise RuntimeError(f"Error while searching for gage_id in layer '{layer_name}' "
                               f"from {_pp(orig_path, local_path)}: {e}")


def validate_catchments_in_layer(gpkg_path: str, layer_name: str) -> list[str]:
    """
    Extract catchment identifiers from a layer that contains a 'divide_id' column.

    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param layer_name: Name of the layer to inspect.
    :return: List of catchment IDs as strings, or an empty list if the field is not present.
    :raises RuntimeError: If the layer cannot be read.
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            gdf = gpd.read_file(local_path, layer=layer_name)
            if 'divide_id' in gdf.columns:
                return gdf['divide_id'].astype(str).tolist()
            else:
                return []
        except Exception as e:
            raise RuntimeError(f"Failed to validate catchments in layer '{layer_name}' "
                               f"from {_pp(orig_path, local_path)}: {e}")


def find_catchments(gpkg_path: str, target_layers: list[str] = ["divides", "catchments", "watersheds"]) -> None:
    """
    Search for catchment geometries across a set of likely layer names and print findings.

    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param target_layers: Ordered list of candidate layer names to inspect for catchments.
    :return: None. Prints results to stdout.
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            layers = list_layers(local_path)
            for layer in target_layers:
                if layer in layers:
                    logger.info('')
                    logger.info(f"Checking for catchments in layer '{layer}' from {_pp(orig_path, local_path)}:")
                    catchments = validate_catchments_in_layer(local_path, layer)
                    if catchments:
                        logger.info(f"  Found {len(catchments)} catchments in layer '{layer}'.")
                        logger.info(f"  Catchments: {', '.join(catchments)}")
                        return
                    else:
                        logger.info(f"  No catchments found in layer '{layer}'.")
            logger.info("\nNo catchments found in the specified layers.")
        except Exception as e:
            logger.info(f"Error while searching for catchments from {_pp(orig_path, local_path)}: {e}")
            traceback.print_exc()


def display_layer_metadata(gpkg_path: str, layer_name: str) -> None:
    """
    Print metadata and a sample of records from a specific layer.

    Accepts a local path or a remote URL (downloaded to the persistent cache first).

    :param gpkg_path: Path/URL to the GeoPackage file.
    :param layer_name: Name of the layer to inspect.
    :return: None. Outputs metadata and data sample to stdout.
    :raises RuntimeError: If the layer cannot be loaded.
    """
    with _localize_gpkg(gpkg_path) as (orig_path, local_path):
        try:
            gdf = gpd.read_file(local_path, layer=layer_name)
            logger.info(f"Layer '{layer_name}' metadata for {_pp(orig_path, local_path)}:")
            logger.info(gdf.info())
            logger.info("\nSample data:")
            logger.info(gdf.head())
        except Exception as e:
            raise RuntimeError(f"Failed to read metadata for layer '{layer_name}' from {_pp(orig_path, local_path)}: {e}")
