import inspect
import logging
import os
from functools import cache  # Python 3.9+

logger = logging.getLogger(__name__)


@cache  # Cache the project root lookup
def get_project_root() -> str:
    """
    Finds and returns the root directory of the Django project by searching upwards
    from this file's location until it finds 'manage.py'.

    This avoids importing Django settings and provides a reliable way to compute
    paths relative to the project root.

    The result is cached.
    """
    current_dir: str = os.path.dirname(os.path.abspath(__file__))  # Start from this file's directory

    while current_dir != os.path.dirname(current_dir):  # Traverse up until the root
        if "manage.py" in os.listdir(current_dir):
            return current_dir  # Found the project root
        current_dir = os.path.dirname(current_dir)  # Go up one level

    return os.getcwd()  # Fallback if manage.py is not found (shouldn't happen)


# Cached project base directory
BASE_DIR = get_project_root()


def called_from() -> str:
    """
    Returns a string indicating which function called the caller of this function,
    along with the filename and line number, relative to the project root.

    Useful for detailed debug logging.
    """
    stack = inspect.stack()

    if len(stack) < 3:
        return "Unknown caller (not enough stack frames)"

    caller_frame = stack[2]  # The function that called the function calling `called_from()`
    caller_name = caller_frame.function
    caller_filename = caller_frame.filename
    caller_lineno = caller_frame.lineno

    # Convert absolute path to relative path, ensuring it's within BASE_DIR
    relative_path = os.path.relpath(caller_filename, BASE_DIR)

    return f'called from {caller_name} in {relative_path}:{caller_lineno}'


def get_caller_name() -> str:
    """
    Returns the name of the view function that directly called this method.

    This skips the first frame (this function) and returns the caller's name,
    unwrapping common decorators like @api_view to reveal the original view name.
    """
    frame = inspect.currentframe()
    if frame is not None:
        frame = frame.f_back  # Go up one frame to the caller
        if frame is not None:
            func_name = frame.f_code.co_name

            # Attempt to unwrap common decorator patterns
            maybe_self = frame.f_locals.get('self') or frame.f_locals.get('func') or frame.f_locals.get('view_func')
            if maybe_self and hasattr(maybe_self, '__name__'):
                return maybe_self.__name__

            return func_name

    return "unknown"
