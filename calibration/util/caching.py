"""
caching.py

Centralized caching utilities for static model data (Modules, Gages, OptimizationInputs, etc.)
used throughout calibration, validation, and forecast processes.

Caching Strategy
----------------
All data loaded here is static for the lifetime of the server process. To minimize
database access and redundant serialization across Gunicorn workers, we use a two-layer
approach:

1. **@lru_cache (in-memory per worker)**
   - Keeps frequently accessed data resident in each worker’s memory.
   - Prevents repeated lookups in the Django cache layer.
   - Ideal for static data since it never changes during runtime.

2. **Django file-based cache (shared across workers)**
   - Configured in `settings.py` using:
         CACHES = {
             "default": {
                 "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
                 "LOCATION": "/tmp/django_cache",
             }
         }
   - Stores serialized cache entries on disk so all Gunicorn workers share the same
     underlying data without re-querying the database.
   - Provides consistency across workers with negligible overhead.

Behavior Summary
----------------
- On first access, the function checks the Django file-based cache.
- If no entry exists, it queries the database and writes the result to disk.
- The @lru_cache layer keeps that data in RAM for subsequent access in the same worker.
- Because the data is static for the life of the process, no invalidation logic is needed.

This pattern ensures:
- Shared cache state across Gunicorn workers
- In-memory speed after the first lookup
- No external dependencies (no Redis or Memcached required)
"""

import json
from functools import lru_cache

from django.core.cache import cache

from calibration.enums import PlotDefinitionsEnum
from calibration.enums_vanilla import JobType
from calibration.models import Module, ModuleGroup, Gage, CalibrationRun, ValidationRun, ForecastRun, CalibrationFormulation, OptimizationInput


@lru_cache(maxsize=1)
def get_cached_modules_by_id() -> dict[int, Module]:
    """
    Canonical accessor: ID → Module (authoritative module cache)

    Caching strategy:
    - FIRST, we pull from Django's file-based cache (shared across Gunicorn workers).
    - THEN we memoize the result with @lru_cache so this worker does not re-read from disk.
      (Each worker gets its own in-memory copy — safely isolated.)

    :return: Dict mapping {module.id → fully hydrated Module instance}.
    """
    modules_by_name = get_cached_modules_with_groups()  # shared on disk, hydrated once per worker
    return {m.id: m for m in modules_by_name.values()}


def get_cached_module_by_name(module_name: str) -> Module | None:
    """
    Convenience lookup: name → Module

    We DO NOT directly hit Django's file cache here.
    Instead, we derive from the canonical ID-based in-memory cache.
    This guarantees consistency and avoids duplicate disk reads.

    :param module_name: Exact name of module to fetch.
    :return: Module instance, or None if not found.
    """
    modules_by_id = get_cached_modules_by_id()  # single source of truth (per-worker @lru_cached)
    modules_by_name = {m.name: m for m in modules_by_id.values()}  # derived lightweight view
    return modules_by_name.get(module_name)


CACHED_GAGES_KEY = 'cached_gages'


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

    gages_lookup = cache.get(CACHED_GAGES_KEY)
    if not gages_lookup:
        # Fetch from DB and cache results as a dictionary
        gages = Gage.objects.all().values(
            'gage_id', 'agency', 'station_name', 'latitude', 'longitude',
            'altitude', 'nws_id', 'headwater_calibration', 'domain__name', 'is_active'
        )
        gages_lookup = {gage['gage_id']: gage for gage in gages}
        # Adjust key for domain names
        for gage in gages_lookup.values():
            gage['domain'] = gage.pop('domain__name')

        cache.set(CACHED_GAGES_KEY, gages_lookup, timeout=None)
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
        cache.set(CACHED_GAGES_KEY, gages, timeout=None)
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
    cache_key = f'optimization_inputs_{optimization_name}'
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


CACHED_MODULES_KEY = "cached_modules_with_groups"


@lru_cache(maxsize=1)
def get_cached_modules_with_groups() -> dict[str, Module]:
    """
    Retrieve all active Module ORM objects with prefetched groups/output_variables,
    cached so that no further DB hits occur when accessing relationships.

    - Cached globally in Django cache and also with lru_cache.
    - Prefetch ensures groups and output_variables can be accessed without new queries.
    - Fully safe to reuse for UI display, validations, or parameter resolution.

    :return Returns a dict keyed by module name.
    """
    modules = cache.get(CACHED_MODULES_KEY)
    if modules is None:
        # Eagerly load everything needed (no lazy lookups later)
        qs = (
            Module.objects.filter(is_active=True)
            .prefetch_related("groups", "output_variables")
            .only("id", "name", "display_name", "description", "is_active")
        )
        modules = {m.name: m for m in qs}
        # Force evaluate groups/output_variables to avoid lazy loading
        for m in modules.values():
            list(m.groups.all())
            list(m.output_variables.all())
        cache.set(CACHED_MODULES_KEY, modules, timeout=None)
    return modules


MODULE_GROUPS_CACHE_KEY = 'cached_module_groups'


def get_cached_module_groups() -> list[str]:
    """
    Retrieve a list of active module group names, ordered by 'order',
    cached to avoid repeated queries.

    - Cached in Django cache under MODULE_GROUPS_CACHE_KEY.
    - Ordered by the 'order' field from the DB.

    :return: List of module group names (strings).
    """
    module_groups = cache.get(MODULE_GROUPS_CACHE_KEY)
    if module_groups is None:
        qs = ModuleGroup.objects.filter(is_active=True).order_by("order").only("id", "name", "order")
        # Force eval to freeze them in cache
        module_groups = [mg.name for mg in qs]
        cache.set(MODULE_GROUPS_CACHE_KEY, module_groups, None)
    return module_groups


def get_filtered_plot_definitions(
        run: CalibrationRun | ValidationRun | ForecastRun, plot_name: str | None = None, first_match: bool = False
) -> list[dict] | dict | None:
    """
    Retrieve filtered plot definitions for the specified run and plot name, with a case-insensitive match.

    Behavior:
    - ForecastRun → only forecast plots.
    - ValidationRun or CalibrationRun with automatic_validation → validation plots included.
    - CalibrationRun with LSTM module → only plots with lstm_flag=True.
    - Otherwise → plots must have a valid_optimizations list containing run.optimization.name.

    :param run: The run object, which could be a calibration, validation, or forecast run.
    :param plot_name: The name of the plot to filter by (case-insensitive), or None to retrieve all valid plots.
    :param first_match: If True, returns only the first matching plot definition as a dictionary, or None if no match.
    :return: A list of dictionaries representing plot definitions that match the criteria, a single dictionary if first_match is True, or None if no match is found.
    """
    cached_plot_definitions = PlotDefinitionsEnum.get_choices_with_fields(
        fields=['name', 'display_name', 'description', 'valid_optimizations', 'job_type', 'location', 'filename_mask', 'timeseries_available',
                'lstm_flag']
    )

    have_LSTM_flag = have_LSTM(run if isinstance(run, CalibrationRun) else run.calibration_run)

    plot_name_lower = plot_name.lower() if plot_name else None

    def matches_common_criteria(plot: dict) -> bool:
        return (
                (plot_name is None or plot['name'].lower() == plot_name_lower)
                and (
                        plot['job_type'] == JobType.CALIBRATION.value or
                        (include_validation_plots and plot['job_type'] == JobType.VALIDATION.value)
                )
        )

    if isinstance(run, ForecastRun):
        # Only return plots for Forecast jobs
        filtered_plots = [
            plot for plot in cached_plot_definitions
            if (plot_name is None or plot['name'].lower() == plot_name.lower())
               and plot['job_type'] == JobType.FORECAST.value
        ]
    else:
        # Determine if validation plots should be included
        include_validation_plots = isinstance(run, ValidationRun) or (
                isinstance(run, CalibrationRun) and run.automatic_validation
        )

        optimization = run.optimization if isinstance(run, CalibrationRun) else run.calibration_run.optimization

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
