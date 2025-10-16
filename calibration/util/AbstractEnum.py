from enum import Enum
from typing import Any, Generic
from typing import Type, TypeVar

from django.core.cache import cache
from django.db import models

# Create a generic type variable for models and Enums
T = TypeVar('T', bound=models.Model)
E = TypeVar('E', bound='AbstractEnum')


class AbstractEnum(Generic[T], Enum):
    """
    This abstract class provides an enum-like interface that is dynamically synced with the database for complex enums.
    Additionally, it supports simple enums that only require a `get_names()` method.

    - This class allows specific enum members to be defined for values referenced directly in the code,
      reducing the risk of typos or mismatches. These specific members must be present in the database.

    - In addition to explicitly defined members, this class dynamically loads additional values
      from the database and caches them. This ensures that all relevant database values are accessible
      without repeated queries.

    - For complex enums, subclasses should define a `get_model()` method to specify the associated model and may
      override `get_filter()` for custom filtering.
    - For simple enums, no model or filter is needed; only `get_names()` is required.
    """

    @classmethod
    def get_model(cls) -> Type[T] | None:
        """
        Returns the model associated with this enum, if applicable.
        Subclasses should override this method for database-synced enums.
        """
        return None

    @classmethod
    def get_aliases(cls: type[E]) -> dict[E, list[str]]:
        """
        Optionally overridden by subclasses to provide aliases for enum members.

        :return: A dictionary of aliases for enum members, defaulting to an empty dictionary.
        """
        return {}

    @classmethod
    def _get_cached_items(cls) -> dict[str, T] | None:
        """
        Helper method to retrieve cached items, reloading them from the database if the cache is empty.
        Only applies if a model is defined.

        :return: A dictionary of items cached by name, either from cache or reloaded from the database,
                 or None if there is no model.
        """
        if not cls.get_model():
            return None

        items = cache.get(f'{cls.__name__}_cache')
        if items is None:
            cls.load_items()
            items = cache.get(f'{cls.__name__}_cache')
        return items

    @classmethod
    def get_names(cls) -> list[str]:
        """
        Returns a list of names for the enum values.

        - For simple enums, returns values directly from the enum.
        - For database-synced enums, retrieves names from cached items.

        :return: A list of names for the enum items.
        """
        model = cls.get_model()
        if model is None:
            # Simple enum: return enum values directly
            return [e.value for e in cls]

        # Database-synced enum: return names from cached items
        items = cls._get_cached_items()
        # noinspection PyUnresolvedReferences
        return [item.name for item in items.values()] if items else []

    @classmethod
    def get_all_valid_names(cls) -> list[str]:
        """
        Retrieves all valid names for the enum, including any aliases defined by the subclass.
        Supports flexibility by allowing multiple names (aliases) for the same enum member.

        :return: A list of valid names, including aliases.
        """
        # Retrieve the valid names from the enum itself
        valid_names = set(cls.get_names())
        aliases = cls.get_aliases()  # Call the optional get_aliases() method

        for main_value, alias_list in aliases.items():
            if main_value.value in valid_names:
                valid_names.update(alias_list)

        return list(valid_names)

    @classmethod
    def get_filter(cls) -> dict[str, Any] | None:
        """
        Optional: Subclasses can override this to specify custom filters (e.g., `{'is_active': True}`)
        to apply when loading items from the database.

        By default, no filters are applied.

        :return: A dictionary of filters or None if no filters are needed.
        """
        return None

    @classmethod
    def load_items(cls) -> None:
        """
        Loads items from the database, applying filters specified in `get_filter()` and caches them.
        Only called if a model is defined.
        """
        # Get the model defined in the subclass (e.g., Status)
        model = cls.get_model()

        if model:
            # Get any filter criteria specified in the subclass
            filter_criteria = cls.get_filter() or {}

            # Query the model using the filter criteria and build a dictionary of items keyed by name
            items = model.objects.filter(**filter_criteria)
            # noinspection PyUnresolvedReferences
            item_dict = {item.name: item for item in items}

            # Store the item dictionary in cache
            cache.set(f'{cls.__name__}_cache', item_dict, timeout=None)

    @classmethod
    def get_instance(cls, name: str) -> T:
        """
        Retrieves the model instance corresponding to the given name or alias from the cache,
        reloading from the database if necessary. Raises a ValueError if the name is not found.

        Only applicable if a model is defined.

        :param name: The name or alias of the item to retrieve.
        :return: The model instance associated with the name.
        :raises: ValueError if no matching name or alias exists in the cache.
        """
        if not cls.get_model():
            raise ValueError(f"{cls.__name__} does not support get_instance() without a model.")

        name = name.lower()
        items = cls._get_cached_items()

        # Create a lookup dictionary with lowercase names for case-insensitive retrieval
        items_lower = {item_name.lower(): item for item_name, item in items.items()}

        # Include aliases in the lookup dictionary
        for main_value, alias_list in cls.get_aliases().items():
            main_item = items_lower.get(main_value.value.lower())  # Find the main item
            if main_item:
                items_lower.update({alias.lower(): main_item for alias in alias_list})

        # Retrieve the instance by lowercase name or raise an error if not found
        instance = items_lower.get(name)
        if instance is None:
            # Raise an error if the value isn't found in the cache.
            # We would also get this error if we applied a filter (such as is_active=True)
            raise ValueError(f"No matching database entry for value '{name}' in {cls.__name__}.")

        return instance

    @property
    def db_instance(self) -> T:
        """
        Returns the database model instance associated with this enum member.

        This property retrieves the corresponding database row (model instance)
        for the current enum member based on its `value`. The lookup is performed
        using the `get_instance` method, which ensures that the data is retrieved
        from the cache or, if necessary, loaded from the database.

        Example Usage:
            # Access the database instance for the DONE status
            done_instance = StatusEnum.DONE.db_instance

        :return: The model instance associated with the current enum member.
        :raises ValueError: If the enum class is not linked to a database model or
                            if the corresponding model instance cannot be found.
        """
        return self.__class__.get_instance(self.value)

    @classmethod
    def from_name(cls: type[E], name: str) -> E:
        """
        Returns the enum member corresponding to the given name or alias.
        Raises ValueError if no match is found.
        """
        name = name.lower()

        # Check direct matches
        for member in cls:
            if member.value.lower() == name:
                return member

        # Check aliases
        for main_value, alias_list in cls.get_aliases().items():
            if any(alias.lower() == name for alias in alias_list):
                return main_value

        raise ValueError(f"No matching enum member for name '{name}' in {cls.__name__}")

    @classmethod
    def get_choices_with_fields(cls, fields: list[str] = None, extra_filter: dict[str, Any] = None) -> list[dict[str, Any]]:
        """
        Returns a list of items from the database, including only the specified fields in each item
        (defaults to 'name' and 'description'). This method is useful for front-end selections.

        :param fields: A list of fields to include for each item (e.g., ['name', 'description']).
        :param extra_filter: A dictionary of additional filters to apply temporarily.
        :return: A list of dictionaries, where each dictionary contains the requested fields
                 for an item (e.g., [{'name': 'Running', 'description': '...'}])
        """
        if fields is None:
            # Default to including the 'name' and 'description' fields
            fields = ['name', 'description']

        items = cls._get_cached_items() or {}

        if extra_filter:
            # Apply extra filtering dynamically
            items = {k: v for k, v in items.items() if all(getattr(v, key, None) == value for key, value in extra_filter.items())}

        # Return each item as a dictionary of the specified fields
        return [
            {field: getattr(item, field, None)
             for field in fields} for item in items.values()
        ]
