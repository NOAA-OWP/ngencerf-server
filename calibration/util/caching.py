"""
caching.py

Centralized caching utilities for model data and generated configuration data
used throughout calibration, validation, and forecast workflows.

Caching Strategy
----------------
Redis is used as the Django cache backend. All application workers share the
same cached values.

Behavior Summary
----------------
- Cache lookups use Django's configured cache backend.
- On a cache miss, functions load data from the database, enums, or another
  appropriate source and store the result in Redis.
- Since Redis is shared across workers, data loaded by one worker is available
  to all workers.
- Most cached reference data is stored indefinitely because it changes rarely
  during a deployment.
- Some utilities explicitly update cached values or regenerate local files when
  required.

This provides:
- Shared high-speed caching across workers
- Reduced database and initialization work
- No unnecessary per-worker duplication
- Consistent cached reference data
"""
import json
import logging
import os
from typing import Any, Literal, overload

import yaml
from django.conf import settings
from django.core.cache import cache

from calibration.enums import PlotDefinitionsEnum, ForecastConfigEnum, JobType, HindcastConfigEnum
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

    :return: Returns a dict keyed by module name.
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

    - Cached in Django cache under _MODULE_GROUPS_CACHE_KEY.
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
    Return the cached modules keyed by module ID.

    This derives the ID-based lookup from the canonical name-based module cache
    returned by ``get_cached_modules_with_groups()``.

    :return: Dictionary mapping module IDs to fully hydrated Module instances.
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
    Retrieve ModuleProperty ORM objects with the fields and module relationship
    needed by current callers.

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
    Retrieve ordered ModulePropertyChoice ORM objects from the shared cache.

    The rows are treated as static for the cache lifetime and are stored in
    module-property and display order. Only fields needed by current callers
    are loaded.

    :return: Module-property choices ordered by module property, sort order,
             and ID.
    """
    choices = cache.get(_CACHED_MODULE_PROPERTY_CHOICES_KEY)

    if choices is None:
        choices = list(
            ModulePropertyChoice.objects
            .only(
                "id",
                "module_property_id",
                "label",
                "description",
                "sort_order",
                "value_int",
                "value_str",
            )
            .order_by(
                "module_property_id",
                "sort_order",
                "id"
            )
        )

        cache.set(
            _CACHED_MODULE_PROPERTY_CHOICES_KEY,
            choices,
            timeout=None
        )

    return choices


_CACHED_GAGES_KEY = f"{CACHE_PREFIX}cached_gages"


def get_cached_gages() -> dict[str, dict[str, str | float | int | None]]:
    """
    Retrieve all gages from cache, falling back to the database on a cache miss.

    - Cached under ``_CACHED_GAGES_KEY`` in Django's cache.
    - Includes active and inactive gages so callers can inspect or update
      ``is_active``.
    - Includes all gage attributes needed for lookups and display.
    - Normalizes ``domain__name`` to ``domain``.
    - Stores the results as dictionaries rather than ORM objects.

    :return: Dictionary mapping each gage ID to its cached field values.

    """
    # Return the shared cached lookup when it has already been populated.

    gages_lookup = cache.get(_CACHED_GAGES_KEY)
    if gages_lookup is None:
        # Fetch all gages and cache them as a dictionary keyed by gage ID.
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

    This updates only the cached value; it does not update the database.

    - If ``is_active`` is provided, the cached value is updated when necessary.
    - If ``is_active`` is None, the current cached value is returned.
    - Returns None when the gage ID is not present in the cached lookup.

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
    Retrieve filtered plot definitions for the specified run and optional plot
    name.

    Filtering behavior:
      - Include calibration and validation plot definitions.
      - For runs using an LSTM module, include only plots with
        ``lstm_flag=True``.
      - For other runs, include only plots whose ``valid_optimizations`` list
        contains the run's optimization name.
      - Match ``plot_name`` case-insensitively when it is provided.

    :param run: Calibration or validation run for which plots are requested.
    :param plot_name: Optional plot name to match case-insensitively.
    :param first_match: If True, return only the first matching definition.
    :return: A list of matching plot definitions, or the first matching
             definition when ``first_match`` is True. Returns None when
             ``first_match`` is True and no match exists.
    """
    cached_plot_definitions = PlotDefinitionsEnum.get_choices_with_fields(
        fields=[
            "name",
            "display_name",
            "description",
            "valid_optimizations",
            "job_type",
            "location",
            "filename_mask",
            "timeseries_available",
            "lstm_flag",
        ]
    )

    have_lstm_flag = have_LSTM(
        run if isinstance(run, CalibrationRun) else run.calibration_run
    )

    plot_name_lower = plot_name.lower() if plot_name else None

    # Both supported run types include calibration and validation plots.
    include_validation_plots = isinstance(run, ValidationRun) or isinstance(
        run,
        CalibrationRun,
    )

    optimization = (
        run.optimization
        if isinstance(run, CalibrationRun)
        else run.calibration_run.optimization
    )

    def matches_common_criteria(plot: dict[str, Any]) -> bool:
        return (
                (
                        plot_name_lower is None
                        or plot["name"].lower() == plot_name_lower
                )
                and (
                        plot["job_type"] == JobType.CALIBRATION.value
                        or (
                                include_validation_plots
                                and plot["job_type"] == JobType.VALIDATION.value
                        )
                )
        )

    if have_lstm_flag:
        filtered_plots = [
            plot
            for plot in cached_plot_definitions
            if (
                    matches_common_criteria(plot)
                    and plot.get("lstm_flag", False) is True
            )
        ]
    else:
        # Standard case: filter by valid_optimizations
        filtered_plots = [
            plot
            for plot in cached_plot_definitions
            if (
                    matches_common_criteria(plot)
                    and plot["valid_optimizations"] is not None
                    and optimization.name
                    in json.loads(plot["valid_optimizations"])
            )
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
    Generate a YAML mapping of forecast configurations when it is not already
    available for the current cache and local filesystem state.

    A Redis cache flag avoids unnecessary regeneration across workers, while the
    local file-existence check ensures that each container has the required file.

    Example output:
        short_range: [0, 23, 1, 18, 1]
        short_range_hawaii: [0, 12, 12, 48, 0.25]

    Configuration values are obtained from the supplied enum class without a
    database query.

    :param enum_class: ForecastConfigEnum for active forecast configurations, or
                       HindcastConfigEnum for hindcast-supported configurations.
    :return: Full path to the generated configuration YAML file.
    """
    is_hindcast = enum_class is HindcastConfigEnum

    file_name = "hindcast_configurations.yaml" if is_hindcast else "forecast_configurations.yaml"
    output_file = os.path.join(settings.NGEN_VERIFICATION_WORK_DIR, file_name)

    cache_key = (
        f"{_FORECAST_CFG_FILE_CACHE_KEY_BASE}_hindcast"
        if is_hindcast
        else f"{_FORECAST_CFG_FILE_CACHE_KEY_BASE}_forecast"
    )

    # Reuse the generated file only when both the shared cache flag and this
    # container's local file are present.
    if cache.get(cache_key) and os.path.isfile(output_file):
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
