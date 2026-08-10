"""
caching.py

Centralized caching utilities for static model data (Modules, Gages, OptimizationInputs, etc.)
used throughout calibration, validation, and forecast processes.

Caching Strategy
----------------
Redis is used as the Django cache backend. All workers share the same cache

Behavior Summary
----------------
- All cache lookups use Django’s Redis backend.
- On cache miss, functions query the DB and store serialized data in Redis.
- Since Redis is shared across workers, the first load benefits all workers.
- No invalidation logic is required because this data is static for the lifetime
  of the server.

This ensures:
- Shared high-speed caching across workers
- No per-worker duplication of memory
- Clean and consistent cached state
"""
import json
import os

import yaml
from django.conf import settings
from django.core.cache import cache

from calibration.enums import PlotDefinitionsEnum, ForecastConfigEnum
from calibration.enums_vanilla import JobType
from calibration.models import Module, ModuleGroup, Gage, CalibrationRun, ValidationRun, CalibrationFormulation, OptimizationInput
from calibration.views.cache_prefix import CACHE_PREFIX

_CACHED_MODULES_KEY = f"{CACHE_PREFIX}cached_modules_with_groups"


def get_cached_modules_with_groups() -> dict[str, Module]:
    """
    Retrieve all active Module ORM objects with prefetched groups/output_variables,
    cached so that no further DB hits occur when accessing relationships.

    Redis-backed cache provides global sharing across workers.

    :return Returns a dict keyed by module name.
    """
    modules = cache.get(_CACHED_MODULES_KEY)
    if modules is None:
        # Eagerly load everything needed (no lazy lookups later)
        qs = (
            Module.objects.filter(is_active=True)
            .prefetch_related("groups", "output_variables")
            .only("id", "name", "display_name", "description", "is_active")
        )
        modules = {m.name: m for m in qs}

        # Force evaluate related fields to avoid lazy lookups
        for m in modules.values():
            list(m.groups.all())
            list(m.output_variables.all())

        cache.set(_CACHED_MODULES_KEY, modules, timeout=None)

    return modules


_MODULE_GROUPS_CACHE_KEY = f"{CACHE_PREFIX}cached_module_groups"


def get_cached_module_groups() -> list[str]:
    """
    Retrieve a list of active module group names, ordered by 'order',
    cached to avoid repeated queries.

    - Cached in Django cache under MODULE_GROUPS_CACHE_KEY.
    - Ordered by the 'order' field from the DB.

    :return: List of module group names (strings).
    """
    module_groups = cache.get(_MODULE_GROUPS_CACHE_KEY)
    if module_groups is None:
        qs = ModuleGroup.objects.filter(is_active=True).order_by("order").only("id", "name", "order")
        # Force eval to freeze them in cache
        module_groups = [mg.name for mg in qs]
        cache.set(_MODULE_GROUPS_CACHE_KEY, module_groups, None)
    return module_groups


def get_cached_modules_by_id() -> dict[int, Module]:
    """
    Canonical accessor: ID → Module (authoritative module cache)

    Redis provides shared global state

    :return: Dict mapping {module.id → fully hydrated Module instance}.
    """
    modules_by_name = get_cached_modules_with_groups()
    return {m.id: m for m in modules_by_name.values()}


def get_cached_module_by_name(module_name: str) -> Module | None:
    """
    Convenience lookup: name → Module

    We DO NOT directly hit the cache backend here.
    Instead, we derive from the canonical name-based module cache.

    :param module_name: Exact name of module to fetch.
    :return: Module instance, or None if not found.
    """
    modules_by_id = get_cached_modules_by_id()  # derived from the shared Redis-backed module cache
    modules_by_name = {m.name: m for m in modules_by_id.values()}  # derived lightweight view
    return modules_by_name.get(module_name)


_CACHED_GAGES_KEY = f"{CACHE_PREFIX}cached_gages"


def get_cached_gages() -> dict[str, dict[str, str | float | int | None]]:
    """
    Retrieve all active Gages from cache, falling back to the database on cache miss.

    - Cached under CACHED_GAGES_KEY in Django cache.
    - Includes all gage attributes needed for lookups and display.
    - Domain is normalized to 'domain' (instead of 'domain__name').
    - Results are frozen as dicts (not ORM objects) for cheap reuse.

    :return: Dict mapping {gage_id → dict of gage fields}.
    """
    # Check if the gages are already cached

    gages_lookup = cache.get(_CACHED_GAGES_KEY)
    if gages_lookup is None:
        # Fetch from DB and cache results as a dictionary
        gages = Gage.objects.all().values(
            'gage_id', 'agency', 'station_name', 'latitude', 'longitude',
            'altitude', 'nws_id', 'headwater_calibration', 'domain__name', 'is_active'
        )
        gages_lookup = {gage['gage_id']: gage for gage in gages}
        # Adjust key for domain names
        for gage in gages_lookup.values():
            gage['domain'] = gage.pop('domain__name')

        cache.set(_CACHED_GAGES_KEY, gages_lookup, timeout=None)

    return gages_lookup


def update_and_get_cached_gage_status(gage_id: str, is_active: bool | None = None) -> tuple[str, bool] | None:
    """
    Update (or query) the cached 'is_active' flag for a gage.

    - If is_active is provided, update the flag if needed.
    - If is_active is None, just return the current state.
    - Returns (gage_id, current_is_active) in either case.
    - Returns None if the gage_id doesn't exist in the cached map.

    :param gage_id: ID of the gage to update/query
    :param is_active: Desired active state, or None to just query
    :return: A tuple of (gage_id, is_active) reflecting the current state, or None if not found
    """
    gages = get_cached_gages()  # ensures cache is populated

    gage = gages.get(gage_id)
    if not gage:
        return None

    current_status = bool(gage.get('is_active'))
    # If state differs, update and write back
    if is_active is not None and current_status != is_active:
        gages[gage_id] = {**gage, 'is_active': is_active}
        cache.set(_CACHED_GAGES_KEY, gages, timeout=None)
        current_status = is_active

    # Always return (gage_id, current_status)
    return gage_id, current_status


def get_gage_by_id(gage_id: str) -> dict[str, str | float | int | None] | None:
    """
    Retrieve a single gage by gage_id from the cached gages.

    - Uses get_cached_gages() internally.
    - Returns None if gage not found or inactive.
    - Excludes fields not needed by most consumers:
      ['nws_id', 'domain', 'headwater_calibration', 'is_active'].

    :param gage_id: The gage_id to retrieve.
    :return: The gage data if found, otherwise None.
    """
    # Retrieve the gage from the cached set of gages
    gages = get_cached_gages()
    gage = gages.get(gage_id)

    if not gage or not gage.get('is_active'):
        return None

    # Exclude 'nws_id', 'domain', and 'headwater_calibration' from the result
    return {key: value for key, value in gage.items() if key not in ['nws_id', 'domain', 'headwater_calibration', 'is_active']}


def get_cached_optimization_inputs(optimization_name: str) -> list[dict[str, str | int | float]]:
    """
    Retrieve OptimizationInput rows for the given optimization.

    - Cached per optimization_name (using Django cache).
    - Only includes active inputs.
    - Each row is returned as a plain dict with basic fields
      (name, description, data_type, default_value, min, max, id, is_active).

    :param optimization_name: The name of the optimization.
    :return: A list of dictionaries with details of each optimization input (name, description, data_type, etc.).
    """
    cache_key = f"{CACHE_PREFIX}optimization_inputs_{optimization_name}"
    optimization_inputs = cache.get(cache_key)

    # If not in cache, query and cache the results
    if optimization_inputs is None:
        optimization_inputs = list(
            OptimizationInput.objects.filter(optimization__name=optimization_name, is_active=True).values(
                'name', 'description', 'data_type', 'default_value', 'min', 'max', 'id', 'is_active'
            )
        )

        # Cache the inputs
        cache.set(cache_key, optimization_inputs, timeout=None)

    return optimization_inputs


def get_filtered_plot_definitions(
        run: CalibrationRun | ValidationRun, plot_name: str | None = None, first_match: bool = False
) -> list[dict] | dict | None:
    """
    Retrieve filtered plot definitions for the specified run and plot name, with a case-insensitive match.

    Behavior:
    - ValidationRun or CalibrationRun with automatic_validation → validation plots included.
    - CalibrationRun with LSTM module → only plots with lstm_flag=True.
    - Otherwise → plots must have a valid_optimizations list containing run.optimization.name.

    :param run: The run object, which could be a calibration, validation, or forecast run.
    :param plot_name: The name of the plot to filter by (case-insensitive), or None to retrieve all valid plots.
    :param first_match: If True, returns only the first matching plot definition as a dictionary, or None if no match.
    :return: A list of dictionaries representing plot definitions that match the criteria, a single dictionary if first_match is True, or None if no match is found.
    """
    cached_plot_definitions = PlotDefinitionsEnum.get_choices_with_fields(
        fields=['name', 'display_name', 'description', 'valid_optimizations', 'job_type', 'location', 'filename_mask',
                'timeseries_available', 'lstm_flag']
    )

    have_LSTM_flag = have_LSTM(run if isinstance(run, CalibrationRun) else run.calibration_run)

    plot_name_lower = plot_name.lower() if plot_name else None

    # Determine if validation plots should be included
    include_validation_plots = isinstance(run, ValidationRun) or (
            isinstance(run, CalibrationRun) and run.automatic_validation
    )

    optimization = run.optimization if isinstance(run, CalibrationRun) else run.calibration_run.optimization

    def matches_common_criteria(plot: dict) -> bool:
        return (
                (plot_name is None or plot['name'].lower() == plot_name_lower)
                and (
                        plot['job_type'] == JobType.CALIBRATION.value
                        or (include_validation_plots and plot['job_type'] == JobType.VALIDATION.value)
                )
        )

    if have_LSTM_flag:
        # LSTM mode: only include plots with lstm_flag=True
        filtered_plots = [
            plot for plot in cached_plot_definitions
            if matches_common_criteria(plot) and plot.get('lstm_flag', False) is True
        ]
    else:
        # Standard case: filter by valid_optimizations
        filtered_plots = [
            plot for plot in cached_plot_definitions
            if matches_common_criteria(plot)
               and plot['valid_optimizations'] is not None
               and optimization.name in json.loads(plot['valid_optimizations'])
        ]

    # Return the first match if first_match is True, otherwise return the list of matches
    return filtered_plots[0] if first_match and filtered_plots else filtered_plots


def have_LSTM(run: CalibrationRun) -> bool:
    """
    Check if a given calibration run includes an LSTM module.

    - Queries CalibrationFormulation rows (runtime data).
    - Resolves module objects via cached module map (no extra SELECTs on Module).
    - Returns True if any formulation’s module resolves to 'LSTM'.

    :param run: The calibration run instance.
    :return: True if the run includes the LSTM module, False otherwise.
    """
    # Fetch formulations for the run (runtime dynamic data)
    formulations = CalibrationFormulation.objects.filter(
        calibration_run=run
    ).only("module_id")

    modules_by_id = get_cached_modules_by_id()
    return any(modules_by_id[f.module_id].name == "LSTM" for f in formulations if f.module_id in modules_by_id)


_FORECAST_CFG_FILE_CACHE_KEY = f"{CACHE_PREFIX}forecast_config_file_created"


class _FlowSeqDumper(yaml.SafeDumper):
    """
    Custom YAML dumper that keeps mappings in normal block style
    but forces all Python lists to render as inline flow style: [a, b, c].

    This makes the YAML output compact and consistent with formats such as:
        short_range: [0, 23, 1, 18, 1]
        medium_range_mem1: [0, 18, 6, 240, 1]
    """
    pass


def _represent_sequence_flow(dumper, data):
    """
    Override PyYAML's default sequence representation to always use flow style.

    Produces:
        [a, b, c]
    instead of:
        - a
        - b
        - c
    """
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


# Register the custom representer for all Python lists
_FlowSeqDumper.add_representer(list, _represent_sequence_flow)


def generate_forecast_config_yaml() -> str:
    """
    Generate (once per server run) a YAML mapping of forecast configurations.

    Example output:
        short_range: [0, 23, 1, 18, 1]
        short_range_hawaii: [0, 12, 12, 48, 0.25]

    Uses cached ForecastConfiguration data from ForecastConfigEnum (no DB hit).

    :return: Full path of the generated forecast configuration YAML file.
    """
    output_file = os.path.join(settings.NGEN_VERIFICATION_WORK_DIR, "forecast_configurations.yaml")

    # Only generate once per server process
    if cache.get(_FORECAST_CFG_FILE_CACHE_KEY):
        return output_file

    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    configs = ForecastConfigEnum.get_choices_with_fields(
        fields=[
            "internal_name",
            "is_active",
            "cycle_start",
            "cycle_end",
            "cycle_freq",
            "fcst_win",
            "fcst_timestep",
        ]
    )

    yaml_map = {
        cfg["internal_name"]: [
            cfg["cycle_start"],
            cfg["cycle_end"],
            cfg["cycle_freq"],
            cfg["fcst_win"],
            cfg["fcst_timestep"],
        ]
        for cfg in sorted(configs, key=lambda x: x["internal_name"])
        if cfg.get("is_active")
    }

    header_comment = (
        "# For each forecast configuration, provide the following information (in order):\n"
        "# - cycle_start: start time of forecast cycles in Zulu time or UTC (e.g., 0Z)\n"
        "# - cycle_end: end time of forecast cycles in Zulu time or UTC (e.g., 23Z)\n"
        "# - cycle_freq: frequency of forecast cycles in hours (e.g., 1)\n"
        "# - fcst_win: forecast window in hours (e.g., 18)\n"
        "# - fcst_timestep: forecast timestep in hours (e.g., 1)\n"
    )

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header_comment)
        yaml.dump(
            yaml_map,
            f,
            Dumper=_FlowSeqDumper,
            sort_keys=True,
            default_flow_style=False,  # mappings remain in normal block style
            allow_unicode=True,
            width=2048,  # prevent line wrapping inside lists
        )

    cache.set(_FORECAST_CFG_FILE_CACHE_KEY, True, timeout=None)
    return output_file
