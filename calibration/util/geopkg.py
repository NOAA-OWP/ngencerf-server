import logging
import os
import traceback
from functools import lru_cache
from io import BytesIO, StringIO
from itertools import cycle

import fiona
import geopandas as gpd
import matplotlib
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

# See https://stackoverflow.com/questions/27147300/matplotlib-tcl-asyncdelete-async-handler-deleted-by-the-wrong-thread
matplotlib.use('Agg')  # Use a backend that doesn't require a display (like for generating images)

layer_style_config = {
    "nexus": {
        "markersize": 100,
        "color": "blue",
        "plot_method": "point",
    },
    "virtual_nexus": {
        "markersize": 80,
        "color": "blue",
        "plot_method": "point",
    },
    "flowpaths": {  # nhf equivalent of old flowlines
        "linewidth": 2.0,
        "linestyle": "--",  # Use ':' for dotted, '--' for dashed
        "color": "blue",
        "plot_method": "line",
    },
    "flowlines": {  # old format
        "linewidth": 2.0,
        "linestyle": "--",
        "color": "blue",
        "plot_method": "line",
    },
    "virtual_flowpaths": {
        "linewidth": 1.5,
        "linestyle": ":",
        "color": "green",
        "plot_method": "line",
    },
    "waterbodies": {
        "linewidth": 1.0,
        "color": "cyan",
        "plot_method": "line",
    },
    "lakes": {
        "linewidth": 1.0,
        "color": "cyan",
        "plot_method": "line",
    },
    "gages": {
        "markersize": 120,
        "color": "magenta",
        "plot_method": "point",
    },
    "divides": {
        "linewidth": 2.0,
        "color": "black",
        "plot_method": "boundary",  # purely informational
    },
}


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


def safe_read_gpkg(gpkg_path: str, layer: str | None = None) -> gpd.GeoDataFrame:
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
def gpkg_to_png_selected_layers(gpkg_path: str) -> BytesIO:
    """
    Generate a PNG image from selected layers in a local GeoPackage and return it as a BytesIO object.

    The layers to include are intentionally embedded in this function to keep output consistent
    across call sites and avoid "accidental" differences in plots.

    The function attempts to plot a superset of useful hydrofabric layers across:
      - OLD FORMAT geopackages
      - NEW FORMAT (nhf) geopackages

    Any layers that do not exist in the file are automatically skipped.
    Any layers that exist but are non-spatial (no geometry) or empty are also skipped.

    Note: 'divides' is required for a meaningful visualization. If it is missing or unreadable,
    this function raises.

    :param gpkg_path: Local filesystem path to the GeoPackage file.
    :return: BytesIO object containing the generated PNG image.
    :raises FileNotFoundError: If the GeoPackage file does not exist or is not accessible.
    :raises RuntimeError: If the required 'divides' layer is missing/unreadable/unusable.
    """
    check_file_accessible(gpkg_path)

    # Ordered list of additional layers to include in the plot.
    # Layers are skipped automatically if not present in the GeoPackage.
    #
    # IMPORTANT:
    # - Do not include layers that are consistently non-spatial (no usable geometry).
    layers_to_include = (
        "waterbodies",          # nhf
        "lakes",                # older subsets (often empty, harmless)
        "flowpaths",
        "flowlines",            # older variants
        "nexus",
        "gages",                # nhf
    )

    # Initialize the plot
    fig, ax = plt.subplots(figsize=(15, 13), dpi=300)  # Larger figure and higher resolution

    available_layers = list_layers(gpkg_path)

    # Always start with the 'divides' layer (required)
    # We plot only boundaries here because "divides" can be visually dominant as filled polygons.
    if "divides" not in available_layers:
        raise RuntimeError(
            f"Required layer 'divides' not found in '{gpkg_path}'. Available layers: {available_layers}"
        )

    try:
        divides_gdf = safe_read_gpkg(gpkg_path, layer="divides")

        if "geometry" not in divides_gdf.columns or divides_gdf.empty:
            raise RuntimeError(
                f"Required layer 'divides' in '{gpkg_path}' has no geometry or is empty."
            )

        # Simplify geometries for performance improvement
        divides_gdf["geometry"] = divides_gdf["geometry"].simplify(tolerance=0.01, preserve_topology=True)

        # Create boundary GeoDataFrame (Shapely 2.x compatible)
        boundary_gdf = gpd.GeoDataFrame(geometry=divides_gdf.geometry.boundary, crs=divides_gdf.crs)

        style = layer_style_config.get("divides", {})
        boundary_gdf.plot(
            ax=ax,
            color=style.get("color", "black"),
            linewidth=float(style.get("linewidth", 1.5)),
        )
    except Exception as e:
        raise RuntimeError(f"Failed to read or plot required layer 'divides' from '{gpkg_path}'. Error: {e}")

    # Color cycle fallback for layers not in config
    color_cycle = cycle(["blue", "green", "red", "cyan", "magenta"])

    # Plot each requested layer if it exists
    for layer in layers_to_include:
        if layer in available_layers:
            try:
                layer_gdf = safe_read_gpkg(gpkg_path, layer=layer)

                # Skip non-spatial or empty layers (non-fatal)
                if "geometry" not in layer_gdf.columns:
                    logger.warning(f"Skipping layer '{layer}' because it has no geometry column (source: '{gpkg_path}').")
                    continue
                if layer_gdf.empty:
                    logger.warning(f"Skipping layer '{layer}' because it is empty (source: '{gpkg_path}').")
                    continue

                # Simplify geometries for performance improvement
                layer_gdf["geometry"] = layer_gdf["geometry"].simplify(tolerance=0.01, preserve_topology=True)

                style = layer_style_config.get(layer, {})
                if not style:
                    logger.warning(f"No style config for '{layer}'. Using fallback (source: '{gpkg_path}').")

                plot_method = style.get("plot_method", "line")
                color = style.get("color", next(color_cycle))
                linewidth = float(style.get("linewidth", 1.5))
                linestyle = style.get("linestyle", "-")
                markersize = style.get("markersize", 10)

                if plot_method == "point":
                    layer_gdf.plot(ax=ax, color=color, markersize=markersize)
                else:
                    layer_gdf.plot(ax=ax, color=color, linewidth=linewidth, linestyle=linestyle)

            except Exception as e:
                # Non-fatal for optional layers: warn and continue
                logger.warning(f"Skipping layer '{layer}' due to error (source: '{gpkg_path}'): {e}")

    # Remove axes for better visualization
    ax.set_axis_off()

    # Convert the plot to an in-memory PNG file
    img_buffer = BytesIO()
    plt.savefig(img_buffer, format="png", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    img_buffer.seek(0)
    return img_buffer


@lru_cache()
def get_geometry_from_gpkg(gpkg_path: str, catchment_layer: str | None = None, gage_layer: str | None = None) -> dict:
    """
    Extract catchment boundaries (as WKT) and gage coordinates (latitude/longitude) from a local GeoPackage.

    Catchment ID field differences:
      - NEW FORMAT (nhf): divides.div_id
      - OLD FORMAT: divides.divide_id

    Gage location differences:
      - NEW FORMAT (nhf): gages layer typically has point geometry and site_no
      - OLD FORMAT: hydrolocations has hl_x / hl_y (no geometry), and sometimes hl_uri for ID

    :param gpkg_path: Local filesystem path to the GeoPackage file.
    :param catchment_layer: Name of the layer containing catchment boundaries. Defaults to 'divides'.
    :param gage_layer: Fallback gage layer name. Defaults to 'hydrolocations' (old format).
                      If 'gages' exists (nhf), it is preferred automatically.
    :return: Dictionary containing:
             - "catchments": Mapping of catchment identifiers to their boundaries as WKT strings.
             - "gage_coordinates": Dictionary with 'latitude' and 'longitude', or None if not present.
             - "crs": The coordinate reference system of the catchment layer.
    :raises RuntimeError: If reading fails.
    :raises ValueError: If the specified layers or required columns are missing.
    """
    if catchment_layer is None:
        catchment_layer = "divides"
    if gage_layer is None:
        gage_layer = "hydrolocations"

    check_file_accessible(gpkg_path)

    # List all layers to verify the requested layers exist
    available_layers = list_layers(gpkg_path)

    if catchment_layer not in available_layers:
        raise ValueError(
            f"Catchment layer '{catchment_layer}' not found in '{gpkg_path}'. "
            f"Available layers: {available_layers}"
        )

    # Read the catchments layer
    try:
        gdf_catchments = safe_read_gpkg(gpkg_path, layer=catchment_layer)
    except Exception as e:
        raise RuntimeError(f"Failed to read layer '{catchment_layer}' from '{gpkg_path}'. Error: {e}")

    catchments_crs = gdf_catchments.crs

    # Catchment id column can vary between formats.
    # Prefer NEW FORMAT (nhf): div_id, then fall back to OLD FORMAT: divide_id.
    catchment_id_col = None
    for candidate in ("div_id", "divide_id"):
        if candidate in gdf_catchments.columns:
            catchment_id_col = candidate
            break

    if catchment_id_col is None or "geometry" not in gdf_catchments.columns:
        raise ValueError(
            "No supported catchment id column found. Expected one of: div_id, divide_id. "
            f"Columns: {list(gdf_catchments.columns)}"
        )

    catchments = {row[catchment_id_col]: row["geometry"].wkt for _, row in gdf_catchments.iterrows()}

    # Read gage layer
    # Prefer NEW FORMAT (nhf): gages (point geometry)
    # Fall back to OLD FORMAT: hydrolocations (hl_x/hl_y)
    gage_source_layer = "gages" if "gages" in available_layers else gage_layer
    if gage_source_layer not in available_layers:
        # No usable gage layer available; still return catchments
        return {
            "catchments": catchments,
            "gage_coordinates": None,
            "crs": catchments_crs.to_string() if catchments_crs is not None else None
        }

    try:
        gdf_gage = safe_read_gpkg(gpkg_path, layer=gage_source_layer)
    except Exception as e:
        raise RuntimeError(f"Failed to read layer '{gage_source_layer}' from '{gpkg_path}'. Error: {e}")

    # Ensure gage data exists and convert coordinates
    gage_coordinates = None
    if not gdf_gage.empty:
        # NEW FORMAT (nhf): use geometry directly when available
        if "geometry" in gdf_gage.columns and gdf_gage.geometry is not None and not gdf_gage.geometry.is_empty.all():
            if gdf_gage.crs is None and catchments_crs is not None:
                gdf_gage = gdf_gage.set_crs(catchments_crs.to_string())

            gdf_gage_ll = gdf_gage.to_crs(epsg=4326)
            pt = gdf_gage_ll.geometry.iloc[0]
            if pt is not None and not pt.is_empty:
                gage_coordinates = {"latitude": pt.y, "longitude": pt.x}

        # OLD FORMAT: hl_x / hl_y fields
        elif "hl_x" in gdf_gage.columns and "hl_y" in gdf_gage.columns:
            # Convert hl_x, hl_y into a GeoDataFrame
            gdf_gage = gdf_gage.set_geometry(gpd.points_from_xy(gdf_gage.hl_x, gdf_gage.hl_y))

            # Assign CRS from catchments if gage CRS is missing
            if gdf_gage.crs is None:
                if catchments_crs is not None:
                    gdf_gage.set_crs(catchments_crs.to_string(), inplace=True)
                else:
                    raise RuntimeError(
                        "CRS is missing for both the gage and catchments layers in "
                        f"'{gpkg_path}'. Cannot convert to latitude/longitude."
                    )

            # Convert to EPSG:4326 (WGS84 lat/lon)
            gdf_gage = gdf_gage.to_crs(epsg=4326)
            gage_point = gdf_gage.geometry.iloc[0]  # Assuming first entry is the gage
            gage_coordinates = {"latitude": gage_point.y, "longitude": gage_point.x}

    return {
        "catchments": catchments,
        "gage_coordinates": gage_coordinates,
        "crs": catchments_crs.to_string() if catchments_crs is not None else None
    }


def list_layers(gpkg_path: str) -> list[str]:
    """
    Retrieve all layer names from a local GeoPackage file.

    :param gpkg_path: Local filesystem path to the GeoPackage (.gpkg) file.
    :return: A list of layer names available in the file.
    :raises RuntimeError: If the file cannot be opened or read as a GeoPackage.
    """
    try:
        return fiona.listlayers(gpkg_path)
    except fiona.errors.DriverError as e:
        raise RuntimeError(f"Could not open GeoPackage '{gpkg_path}'. It may be corrupted or not a valid file. Error: {e}")
    except Exception as e:
        raise RuntimeError(f"Unexpected error listing layers in '{gpkg_path}': {e}")


def find_gage_id(gpkg_path: str) -> list[str]:
    """
    Extract unique gage IDs from a local GeoPackage, automatically handling both formats.

    Prefer NEW FORMAT (nhf), then fall back to OLD FORMAT.

    NEW FORMAT (nhf):
      - gages.site_no

    OLD FORMAT:
      - hydrolocations.hl_uri

    :param gpkg_path: Local filesystem path to the GeoPackage file.
    :return: List of unique gage ID strings, or an empty list if not found.
    :raises RuntimeError: If a candidate layer exists but cannot be read.
    """
    check_file_accessible(gpkg_path)
    layers = list_layers(gpkg_path)

    # Prefer NEW FORMAT (nhf): gages.site_no
    if "gages" in layers:
        gdf_gages = safe_read_gpkg(gpkg_path, layer="gages")
        if "site_no" in gdf_gages.columns:
            gage_ids = gdf_gages["site_no"].astype(str).unique().tolist()
            logger.info(f"Found gage_id(s) in layer 'gages.site_no': {gage_ids}")
            return gage_ids

    # Fall back to OLD FORMAT: hydrolocations.hl_uri
    if "hydrolocations" in layers:
        gdf_hl = safe_read_gpkg(gpkg_path, layer="hydrolocations")
        if "hl_uri" in gdf_hl.columns:
            gage_ids = gdf_hl["hl_uri"].astype(str).unique().tolist()
            logger.info(f"Found gage_id(s) in layer 'hydrolocations.hl_uri': {gage_ids}")
            return gage_ids

    # Only complain if we fail completely
    logger.info(
        f"Could not find gage_id in '{gpkg_path}'. "
        "Tried NEW FORMAT (nhf): gages.site_no, then OLD FORMAT: hydrolocations.hl_uri."
    )

    return []


def validate_catchments_in_layer(gpkg_path: str, layer_name: str) -> list[str]:
    """
    Extract catchment IDs from a candidate layer.

    Prefer NEW FORMAT (nhf), then fall back to OLD FORMAT.

    NEW FORMAT (nhf):
      - divides.div_id

    OLD FORMAT:
      - divides.divide_id

    :param gpkg_path: Local filesystem path to the GeoPackage file.
    :param layer_name: Name of the layer to inspect.
    :return: List of catchment IDs as strings, or an empty list if no supported field is present.
    :raises RuntimeError: If the layer cannot be read.
    """
    check_file_accessible(gpkg_path)

    try:
        gdf = safe_read_gpkg(gpkg_path, layer=layer_name)

        # Prefer NEW FORMAT (nhf): div_id
        if "div_id" in gdf.columns:
            catchments = gdf["div_id"].astype(str).tolist()
            if catchments:
                return catchments

        # Fall back to OLD FORMAT: divide_id
        if "divide_id" in gdf.columns:
            catchments = gdf["divide_id"].astype(str).tolist()
            if catchments:
                return catchments

        # Only complain if we fail completely
        logger.info(
            f"No catchments found in layer '{layer_name}' for '{gpkg_path}'. "
            "Tried NEW FORMAT (nhf): div_id, then OLD FORMAT: divide_id."
        )
        return []

    except Exception as e:
        raise RuntimeError(f"Failed to validate catchments in layer '{layer_name}' from '{gpkg_path}': {e}")


def find_catchments(gpkg_path: str) -> None:
    """
    Search for catchment geometries across a fixed set of likely layer names and print findings.

    :param gpkg_path: Path to the GeoPackage file.
    :return: None. Prints results to stdout via logger.
    """
    # Ordered list of candidate layer names to inspect for catchments.
    # Keep "divides" first because that is the standard hydrofabric catchment layer.
    target_layers = ["divides", "catchments", "watersheds"]

    try:
        layers = list_layers(gpkg_path)
        for layer in target_layers:
            if layer in layers:
                logger.info("")
                logger.info(f"Checking for catchments in layer '{layer}':")
                catchments = validate_catchments_in_layer(gpkg_path, layer)
                if catchments:
                    logger.info(f"  Found {len(catchments)} catchments in layer '{layer}'.")
                    logger.info(f"  Catchments: {', '.join(catchments)}")
                    return
                else:
                    logger.info(f"  No catchments found in layer '{layer}'.")
        logger.info("\nNo catchments found in the specified layers.")
    except Exception as e:
        logger.info(f"Error while searching for catchments from {gpkg_path}: {e}")
        traceback.print_exc()


def display_layer_metadata(gpkg_path: str, layer_name: str) -> None:
    """
    Print metadata and a sample of records from a specific layer in a local GeoPackage.

    :param gpkg_path: Local filesystem path to the GeoPackage file.
    :param layer_name: Name of the layer to inspect.
    :return: None. Outputs metadata and data sample to stdout.
    :raises RuntimeError: If the layer cannot be loaded.
    """
    check_file_accessible(gpkg_path)

    try:
        gdf = safe_read_gpkg(gpkg_path, layer=layer_name)
        logger.info(f"Layer '{layer_name}' metadata for '{gpkg_path}':")

        buffer = StringIO()
        gdf.info(buf=buffer)
        logger.info(buffer.getvalue())
        logger.info("\nSample data:")
        logger.info(gdf.head())
    except Exception as e:
        raise RuntimeError(f"Failed to read metadata for layer '{layer_name}' from '{gpkg_path}': {e}")
