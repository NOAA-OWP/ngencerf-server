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
import logging
import os
from typing import Any, Literal, overload

import yaml
from django.conf import settings
from django.core.cache import cache

from calibration.enums import PlotDefinitionsEnum, ForecastConfigEnum, HindcastConfigEnum
from calibration.enums_vanilla import JobType
from calibration.models import Module, ModuleGroup, Gage, CalibrationRun, ValidationRun, CalibrationFormulation, OptimizationInput, \
    ModulePropertyChoice, ModuleProperty
from calibration.views.cache_prefix import CACHE_PREFIX

logger = logging.getLogger(__name__)

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
            .only("id", "name", "display_name", "description", "is_active", "use_edfs")
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
    Convenience lookup: name → Module (case-insensitive)

    We DO NOT directly hit the cache backend here.
    Instead, we derive from the canonical name-based module cache.

    :param module_name: Name of module to fetch (case-insensitive).
    :return: Module instance, or None if not found.
    """
    if not module_name:
        return None

    modules_by_name = get_cached_modules_with_groups()  # canonical cache: {name -> Module}

    # Fast path: exact match (keeps behavior for already-correct callers)
    m = modules_by_name.get(module_name)
    if m is not None:
        return m

    # Case-insensitive fallback (O(n), but only when exact key not found)
    target = module_name.casefold()
    for name, module in modules_by_name.items():
        if name.casefold() == target:
            return module

    return None


_CACHED_MODULE_PROPERTIES_KEY = f"{CACHE_PREFIX}cached_module_properties"


def get_cached_module_properties() -> list[ModuleProperty]:
    """
    Retrieve all ModuleProperty ORM objects, fully-hydrated for safe reuse.

    These rows are treated as static for the lifetime of the server, so we cache
    them indefinitely. We select_related('module') and force-access the relation
    to avoid accidental lazy DB hits when callers do p.module.name, etc.

    :return: List of ModuleProperty ORM objects.
    """
    props = cache.get(_CACHED_MODULE_PROPERTIES_KEY)
    if props is None:
        qs = (
            ModuleProperty.objects
            .select_related("module")
            .only("id", "module_id", "module__name", "name", "display_name", "description", "data_type", "default_value")
        )
        props = list(qs)

        # Force evaluate related module to prevent lazy DB hits later
        for p in props:
            _ = p.module.name

        cache.set(_CACHED_MODULE_PROPERTIES_KEY, props, timeout=None)

    return props


_CACHED_MODULE_PROPERTY_CHOICES_KEY = f"{CACHE_PREFIX}cached_module_property_choices"


def get_cached_module_property_choices() -> list[ModulePropertyChoice]:
    """
    Retrieve all ModulePropertyChoice ORM objects, fully-hydrated for safe reuse.

    These rows are treated as static for the lifetime of the server, so we cache
    them indefinitely. We select_related('module_property') and force-access the
    relation to avoid accidental lazy DB hits.

    Ordering: we store them ordered, so callers can group without re-sorting.

    :return: List of ModulePropertyChoice ORM objects.
    """
    choices = cache.get(_CACHED_MODULE_PROPERTY_CHOICES_KEY)
    if choices is None:
        qs = (
            ModulePropertyChoice.objects
            .select_related("module_property")
            .only(
                "id",
                "module_property_id",
                "label",
                "description",
                "sort_order",
                "value_int",
                "value_str",
            )
            .order_by("module_property_id", "sort_order", "id")
        )
        choices = list(qs)

        # Force evaluate related module_property to prevent lazy DB hits later
        for c in choices:
            _ = c.module_property_id

        cache.set(_CACHED_MODULE_PROPERTY_CHOICES_KEY, choices, timeout=None)

    return choices


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


@overload
def get_filtered_plot_definitions(
        run: CalibrationRun | ValidationRun,
        plot_name: str | None = None,
        *,
        first_match: Literal[False] = False,
) -> list[dict[str, Any]]:
    ...


@overload
def get_filtered_plot_definitions(
        run: CalibrationRun | ValidationRun,
        plot_name: str | None = None,
        *,
        first_match: Literal[True],
) -> dict[str, Any] | None:
    ...


def get_filtered_plot_definitions(
        run: CalibrationRun | ValidationRun,
        plot_name: str | None = None,
        *,
        first_match: bool = False
) -> list[dict[str, Any]] | dict[str, Any] | None:
    """
    Retrieve filtered plot definitions for the specified run and plot name, with a case-insensitive match.

    Behavior:
    - ValidationRun or CalibrationRun with automatic_validation → validation plots included.
    - CalibrationRun with LSTM module → only plots with lstm_flag=True.
    - Otherwise → plots must have a valid_optimizations list containing run.optimization.name.

    :param run: The run object, which could be a calibration or validation run.
    :param plot_name: The name of the plot to filter by (case-insensitive), or None to retrieve all valid plots.
    :param first_match: If True, returns only the first matching plot definition as a dictionary, or None if no match.
    :return: A list of dictionaries representing plot definitions that match the criteria, a single dictionary if
        first_match is True, or None if no match is found.
    """
    cached_plot_definitions = PlotDefinitionsEnum.get_choices_with_fields(
        fields=['name', 'display_name', 'description', 'valid_optimizations',
                'job_type', 'location', 'filename_mask',
                'timeseries_available', 'lstm_flag']
    )

    have_lstm_flag = have_LSTM(run if isinstance(run, CalibrationRun) else run.calibration_run)

    plot_name_lower = plot_name.lower() if plot_name else None

    # Determine if validation plots should be included
    include_validation_plots = isinstance(run, ValidationRun) or (
            isinstance(run, CalibrationRun) and run.automatic_validation
    )

    optimization = run.optimization if isinstance(run, CalibrationRun) else run.calibration_run.optimization

    def matches_common_criteria(plot: dict[str, Any]) -> bool:
        return (
                (plot_name_lower is None or plot['name'].lower() == plot_name_lower)
                and (
                        plot['job_type'] == JobType.CALIBRATION.value
                        or (include_validation_plots and plot['job_type'] == JobType.VALIDATION.value)
                )
        )

    if have_lstm_flag:
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

    if first_match:
        return filtered_plots[0] if filtered_plots else None

    return filtered_plots


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

_FORECAST_CFG_FILE_CACHE_KEY_BASE = f"{CACHE_PREFIX}forecast_config_yaml"


def generate_forecast_config_yaml(
        enum_class: type[ForecastConfigEnum] | type[HindcastConfigEnum] = ForecastConfigEnum
) -> str:
    """
    Generate (once per server run, per enum type) a YAML mapping of forecast configurations.

    Example output:
        short_range: [0, 23, 1, 18, 1]
        short_range_hawaii: [0, 12, 12, 48, 0.25]

    Uses cached ForecastConfiguration data from the supplied enum class (no DB hit).

    :param enum_class: ForecastConfigEnum for all active forecast configs,
                       HindcastConfigEnum for hindcast-supported configs only.
    :return: Full path of the generated forecast configuration YAML file.
    """
    is_hindcast = enum_class is HindcastConfigEnum

    file_name = "hindcast_configurations.yaml" if is_hindcast else "forecast_configurations.yaml"
    output_file = os.path.join(settings.NGEN_VERIFICATION_WORK_DIR, file_name)

    cache_key = (
        f"{_FORECAST_CFG_FILE_CACHE_KEY_BASE}_hindcast"
        if is_hindcast
        else f"{_FORECAST_CFG_FILE_CACHE_KEY_BASE}_forecast"
    )

    # Only generate once per server process, per config type
    if cache.get(cache_key):
        return output_file

    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    configs = enum_class.get_choices_with_fields(
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

    cache.set(cache_key, True, timeout=None)
    return output_file
