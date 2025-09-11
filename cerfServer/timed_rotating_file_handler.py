import os
import threading
import time
import traceback
from datetime import timedelta, datetime, timezone
from logging.handlers import BaseRotatingHandler


class CustomTimedRotatingFileHandler(BaseRotatingHandler):
    _emit_lock = threading.Lock()  # Class-level lock for emit/rollover
    """
    A custom log handler that rotates log files at fixed times of day (10:00 and 22:00 UTC, which is 6:00 AM ET and 6:00 PM ET),
    rather than using fixed intervals like the built-in TimedRotatingFileHandler.

    Log files are renamed with a UTC timestamp, and old files are pruned based on backup count.
    """

    def __init__(self, filename, backupCount=7, encoding=None):
        """
        Initialize the handler.

        :param filename: Base log file name.
        :param backupCount: Maximum number of rotated log files to retain.
        :param encoding: Encoding used to open the log file.
        """
        self.backupCount = backupCount
        self.encoding = encoding
        self.utc = True  # Always rotate based on UTC
        self.custom_times = [10, 22]  # Rotation times (hours in UTC)
        self.rolloverAt = self.compute_next_rollover(time.time())

        print_once = not hasattr(CustomTimedRotatingFileHandler, "_printed_rollover_debug")
        if print_once:
            print("Next rollover at:", datetime.fromtimestamp(self.rolloverAt, tz=timezone.utc))
            CustomTimedRotatingFileHandler._printed_rollover_debug = True

        super().__init__(filename, 'a', encoding)

    def compute_next_rollover(self, now_ts):
        """
        Compute the next rollover time in UTC.

        Rolls at 10:00 and 22:00 UTC. If both times have passed today, roll at 10:00 UTC tomorrow.

        :param now_ts: Current time in seconds since epoch.
        :return: Timestamp (float) of the next scheduled rollover.
        """
        now = datetime.fromtimestamp(now_ts, tz=timezone.utc)

        today_rollovers = [
            now.replace(hour=h, minute=0, second=0, microsecond=0)
            for h in self.custom_times
        ]

        for rt in today_rollovers:
            if rt.timestamp() > now_ts:
                return rt.timestamp()

        # All today's rollovers passed; use first time tomorrow
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=self.custom_times[0], minute=0, second=0, microsecond=0).timestamp()

    def shouldRollover(self, record):
        """
        Determine if rollover should occur before writing the next log record.

        :param record: The log record to be written.
        :return: True if current time is past the scheduled rollover time.
        """
        _ = record  # Unused
        t = int(time.time())
        return t >= self.rolloverAt

    def doRollover(self):
        """
        Perform the rollover:
        - Close the current log file.
        - Rename it with the timestamp of the completed interval.
        - Delete old log files beyond backup count.
        - Reopen a new log file.
        - Schedule the next rollover time.
        """
        with self._emit_lock:  # Ensure only one thread rolls over
            try:
                if self.stream:
                    self.stream.close()
                    self.stream = None  # type: ignore[assignment]

                timestamp_str = datetime.fromtimestamp(self.rolloverAt - 1, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')

                # Split base name and extension
                base, ext = os.path.splitext(self.baseFilename)
                rollover_filename = f"{base}.{timestamp_str}{ext}"

                if os.path.exists(self.baseFilename):
                    os.rename(self.baseFilename, rollover_filename)

                # Delete old log files beyond backupCount
                if self.backupCount > 0:
                    dir_name = os.path.dirname(self.baseFilename)
                    base_name = os.path.basename(base)  # base without extension
                    log_files = sorted(
                        [f for f in os.listdir(dir_name)
                         if f.startswith(base_name + ".") and f.endswith(ext)],
                        reverse=True
                    )
                    for old_file in log_files[self.backupCount:]:
                        try:
                            os.remove(os.path.join(dir_name, old_file))
                        except FileNotFoundError:
                            pass

                self.stream = self._open()
                self.rolloverAt = self.compute_next_rollover(time.time())
                self.terminator = '\n'  # Optional: ensure newline

            except Exception:
                # Log the full traceback for debugging
                traceback.print_exc()

    def emit(self, record):
        """
        Emit a log record. Checks for rollover, writes the record, and flushes.
        """
        try:
            with self._emit_lock:
                if self.shouldRollover(record):
                    self.doRollover()

                if self.stream is None:
                    self.stream = self._open()

                msg = self.format(record)
                self.stream.write(msg + self.terminator)
                self.stream.flush()

                # Optional for diagnostics: force sync to disk
                # os.fsync(self.stream.fileno())

        except Exception:
            self.handleError(record)
