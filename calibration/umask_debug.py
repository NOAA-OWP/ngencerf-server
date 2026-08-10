# calibration/umask_debug.py

import logging
import os
import traceback

log = logging.getLogger("umask_trap")

_real_umask = os.umask
_installed = False


def install_umask_trap():
    global _installed
    if _installed:
        return

    def logging_umask(new_mask):
        old_mask = _real_umask(new_mask)

        if new_mask != old_mask:
            # inspect the immediate caller only
            frame = traceback.extract_stack(limit=3)[-2]
            location = f"{frame.filename}:{frame.lineno}"

            log.warning(
                "[umask_diag] os.umask CHANGED %s → %s at %s",
                oct(old_mask), oct(new_mask), location
            )

        return old_mask

    os.umask = logging_umask
    _installed = True
    log.info("umask trap installed")
