import argparse
import os
import json

from calibration.util.geopkg import display_layer_metadata, list_layers, find_catchments, find_gage_id, get_geometry_from_gpkg, \
    gpkg_to_png_selected_layers, safe_read_gpkg, normalize_gpkg


def main():
    parser = argparse.ArgumentParser(description="GeoPackage Utility Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: info
    info_parser = subparsers.add_parser("info", help="Show gage_id, layers, and catchments in a GeoPackage")
    info_parser.add_argument("gpkg_path", type=str, help="Path to the GeoPackage file")
    info_parser.add_argument("--layer", type=str, metavar="LAYER_NAME", help="Display metadata for the specified layer")

    # Subcommand: extract
    extract_parser = subparsers.add_parser("extract", help="Extract geometry and gage info from a GeoPackage")
    extract_parser.add_argument("gpkg_path", type=str, help="Path to the GeoPackage file")
    extract_parser.add_argument("--catchment_layer", type=str, default="divides", help="Layer name for catchments (default: 'divides')")
    extract_parser.add_argument("--gage_layer", type=str, default="hydrolocations", help="Layer name for gages (default: 'hydrolocations')")

    # Subcommand: render
    render_parser = subparsers.add_parser("render", help="Generate PNG from selected layers in a GeoPackage")
    render_parser.add_argument("gpkg_path", type=str, help="Path to the GeoPackage file")
    render_parser.add_argument("png_path", type=str, help="Path to save the generated PNG file")
    render_parser.add_argument("--layers", nargs="+", default=["nexus", "flowpaths", "flowlines"],
                               help="Layers to include in the PNG (default: nexus, flowpaths, flowlines)")

    # Subcommand: normalize
    normalize_parser = subparsers.add_parser("normalize", help="Copy gpkg file while normalizing the CRS")
    normalize_parser.add_argument("gpkg_path", type=str, help="Path to the GeoPackage file")
    normalize_parser.add_argument("output_path", type=str, help="Path to save the new gpkg")

    args = parser.parse_args()

    if not os.path.exists(args.gpkg_path):
        print(f"File {args.gpkg_path} does not exist.")
        return

    try:
        if args.command == "info":
            if args.layer:
                print(f"Displaying metadata for layer '{args.layer}'...")
                display_layer_metadata(args.gpkg_path, args.layer)
            else:
                print("\nSearching for gage_id in the 'hydrolocations' layer:")
                find_gage_id(args.gpkg_path)

                try:
                    divides_gdf = safe_read_gpkg(args.gpkg_path, layer="divides")
                    # crs_proj = divides_gdf.crs.to_string() if divides_gdf.crs else "Unknown"
                    # print(f"\nCRS (PROJ) for 'divides' layer: {crs_proj}")
                    crs_epsg = divides_gdf.crs.to_epsg() if divides_gdf.crs else "Unknown"
                    print(f"\nCRS (EPSG) for 'divides' layer: {crs_epsg}")
                except Exception as e:
                    print(f"Could not retrieve CRS from 'divides' layer: {e}")

                print("\nListing all layers in the GeoPackage:")
                layers = list_layers(args.gpkg_path)
                for layer in layers:
                    print(f"- {layer}")

                find_catchments(args.gpkg_path)

        elif args.command == "geometry":
            result = get_geometry_from_gpkg(
                gpkg_path=args.gpkg_path,
                catchment_layer=args.catchment_layer,
                gage_layer=args.gage_layer,
            )
            print(json.dumps(result, indent=4, default=str))

        elif args.command == "render":
            img = gpkg_to_png_selected_layers(
                gpkg_path=args.gpkg_path,
                layers_to_include=tuple(args.layers)
            )
            with open(args.png_path, "wb") as f:
                f.write(img.getvalue())
            print(f"PNG image saved to: {args.png_path}")

        elif args.command == "normalize":
            normalize_gpkg(args.gpkg_path, args.output_path)

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
