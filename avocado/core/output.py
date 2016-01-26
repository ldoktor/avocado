# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2013-2014
# Author: Lucas Meneghel Rodrigues <lmr@redhat.com>
"""
Manages output and logging in avocado applications.
"""

from StringIO import StringIO
import logging
import os
import re
import sys

from ..utils import path as utils_path
from .settings import settings


if hasattr(logging, 'NullHandler'):
    NULL_HANDLER = logging.NullHandler
else:
    import logutils
    NULL_HANDLER = logutils.NullHandler


STDOUT = _STDOUT = sys.stdout
STDERR = _STDERR = sys.stderr


def early_start():
    """
    Replace all outputs with in-memory handlers
    """
    if os.environ.get('AVOCADO_LOG_DEBUG'):
        add_log_handler("avocado.app.debug", None, STDERR,
                        logging.DEBUG)
    if os.environ.get('AVOCADO_LOG_EARLY'):
        add_log_handler("", None, STDERR, logging.DEBUG)
        add_log_handler("avocado.test", None, STDERR, logging.DEBUG)
    else:
        sys.stdout = StringIO()
        sys.stderr = sys.stdout
        add_log_handler("", MemStreamHandler, None,
                        logging.DEBUG)
    logging.root.level = logging.DEBUG


def enable_stderr():
    """
    Enable direct stdout/stderr (useful for handling errors)
    """
    if hasattr(sys.stdout, 'getvalue'):
        STDERR.write(sys.stdout.getvalue())  # pylint: disable=E1101
    sys.stdout = STDOUT
    sys.stderr = STDERR


def reconfigure(args):
    """
    Adjust logging handlers accordingly to app args and re-log messages.
    """
    # Reconfigure stream loggers
    global STDOUT
    global STDERR
    if getattr(args, "paginator", False) == "on" and is_colored_term():
        STDOUT = Paginator()
        STDERR = STDOUT
    enabled = getattr(args, "show", ["app"])
    if os.environ.get("AVOCADO_LOG_EARLY") and "early" not in enabled:
        enabled.append("early")
    if os.environ.get("AVOCADO_LOG_DEBUG") and "debug" not in enabled:
        enabled.append("debug")
    if getattr(args, "show_job_log", False) and "test" not in enabled:
        enabled.append("test")
    if getattr(args, "silent", False):
        sys.stdout = open(os.devnull, 'w')
        sys.stderr = sys.stdout
        logging.disable(logging.CRITICAL)
        del enabled[:]
        return
    if "app" in enabled:
        app_logger = logging.getLogger("avocado.app")
        app_handler = ProgressStreamHandler()
        app_handler.setFormatter(logging.Formatter("%(message)s"))
        app_handler.addFilter(FilterInfoAndLess())
        app_handler.stream = STDOUT
        app_logger.addHandler(app_handler)
        app_logger.propagate = False
        app_logger.level = logging.DEBUG
        app_err_handler = ProgressStreamHandler()
        app_err_handler.setFormatter(logging.Formatter("%(message)s"))
        app_err_handler.addFilter(FilterWarnAndMore())
        app_err_handler.stream = STDERR
        app_logger.addHandler(app_err_handler)
        app_logger.propagate = False
    else:
        disable_log_handler("avocado.app")
    if not os.environ.get("AVOCADO_LOG_EARLY"):
        logging.getLogger("avocado.test.stdout").propagate = False
        logging.getLogger("avocado.test.stderr").propagate = False
        if "early" in enabled:
            enable_stderr()
            add_log_handler("", None, STDERR, logging.DEBUG)
            add_log_handler("avocado.test", None, STDERR,
                            logging.DEBUG)
        else:
            sys.stdout = STDOUT
            sys.stderr = STDERR
            disable_log_handler("")
            disable_log_handler("avocado.test")
    if "remote" in enabled:
        add_log_handler("avocado.fabric", stream=STDERR)
        add_log_handler("paramiko", stream=STDERR)
    else:
        disable_log_handler("avocado.fabric")
        disable_log_handler("paramiko")
    # Not already enabled by env
    if not os.environ.get('AVOCADO_LOG_DEBUG'):
        if "debug" in enabled:
            add_log_handler("avocado.app.debug", stream=STDERR)
        else:
            disable_log_handler("avocado.app.debug")
    # Add custom loggers
    for name in [_ for _ in enabled if _ not in ["app", "test", "debug",
                                                 "remote", "early"]]:
        name = re.split(r'(?<!\\):', name, maxsplit=1)
        if len(name) == 1:
            name, level = name[0], logging.DEBUG
        else:
            level = (int(name[1]) if name[1].isdigit()
                     else logging.getLevelName(name[1].upper()))
            name = name[0]
        try:
            add_log_handler(name, None, STDERR, level)
        except ValueError, details:
            app_logger.error("Failed to set logger for --output %s:%s: %s.",
                             name, level, details)
            sys.exit(-1)
    # Remove the in-memory handlers
    for handler in logging.root.handlers:
        if isinstance(handler, MemStreamHandler):
            logging.root.handlers.remove(handler)

    # Log early_messages
    for record in MemStreamHandler.log:
        logging.getLogger(record.name).handle(record)


def stop_logging():
    if isinstance(STDOUT, Paginator):
        sys.stdout = _STDOUT
        sys.stderr = _STDERR
        STDOUT.close()


class FilterWarnAndMore(logging.Filter):

    def filter(self, record):
        return record.levelno >= logging.WARN


class FilterInfoAndLess(logging.Filter):

    def filter(self, record):
        return record.levelno <= logging.INFO


class ProgressStreamHandler(logging.StreamHandler):

    """
    Handler class that allows users to skip new lines on each emission.
    """

    def emit(self, record):
        try:
            msg = self.format(record)
            if record.levelno < logging.INFO:   # Most messages are INFO
                pass
            elif record.levelno < logging.WARNING:
                msg = term_support.header_str(msg)
            elif record.levelno < logging.ERROR:
                msg = term_support.warn_header_str(msg)
            else:
                msg = term_support.fail_header_str(msg)
            stream = self.stream
            skip_newline = False
            if hasattr(record, 'skip_newline'):
                skip_newline = record.skip_newline
            stream.write(msg)
            if not skip_newline:
                stream.write('\n')
            self.flush()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)


class MemStreamHandler(logging.StreamHandler):

    """
    Handler that stores all records in self.log (shared in all instances)
    """

    log = []

    def emit(self, record):
        self.log.append(record)

    def flush(self):
        """
        This is in-mem object, it does not require flushing
        """
        pass


class PagerNotFoundError(Exception):
    pass


class Paginator(object):

    """
    Paginator that uses less to display contents on the terminal.

    Contains cleanup handling for when user presses 'q' (to quit less).
    """

    def __init__(self):
        try:
            paginator = "%s -FRSX" % utils_path.find_command('less')
        except utils_path.CmdNotFoundError:
            paginator = None

        paginator = os.environ.get('PAGER', paginator)

        if paginator is None:
            self.pipe = sys.stdout
        else:
            self.pipe = os.popen(paginator, 'w')

    def __del__(self):
        self.close()

    def close(self):
        try:
            self.pipe.close()
        except Exception:
            pass

    def write(self, msg):
        try:
            self.pipe.write(msg)
        except Exception:
            pass


def add_log_handler(logger, klass=None, stream=None, level=None, fmt=None):
    """
    Add handler to a logger.

    :param logger: Name of the target logger
    :param klass: Handler class [`logging.StreamHandler`]
    :param stream: Logging stream (klass argument) [`sys.stdout`]
    :param level: Log level [`INFO`]
    :param fmt: Formatter [`%(name)s: %(message)s`]
    """
    if klass is None:
        klass = logging.StreamHandler
    if stream is None:
        stream = sys.stdout
    if level is None:
        level = logging.INFO
    if fmt is None:
        fmt = '%(name)s: %(message)s'
    console_handler = klass(stream)
    console_handler.setLevel(level)
    if isinstance(fmt, str):
        fmt = logging.Formatter(fmt=fmt)
    console_handler.setFormatter(fmt)
    logging.getLogger(logger).addHandler(console_handler)
    logging.getLogger(logger).propagate = False
    return console_handler


def disable_log_handler(logger):
    logger = logging.getLogger(logger)
    # Handlers might be reused elsewhere, can't delete them
    while logger.handlers:
        logger.handlers.pop()
    logger.handlers.append(NULL_HANDLER())
    logger.propagate = False


def is_colored_term():
    allowed_terms = ['linux', 'xterm', 'xterm-256color', 'vt100', 'screen',
                     'screen-256color']
    term = os.environ.get("TERM")
    colored = settings.get_value('runner.output', 'colored',
                                 key_type='bool')
    if ((not colored) or (not os.isatty(1)) or (term not in allowed_terms)):
        return False
    else:
        return True


class TermSupport(object):

    COLOR_BLUE = '\033[94m'
    COLOR_GREEN = '\033[92m'
    COLOR_YELLOW = '\033[93m'
    COLOR_RED = '\033[91m'
    COLOR_DARKGREY = '\033[90m'

    CONTROL_END = '\033[0m'

    MOVE_BACK = '\033[1D'
    MOVE_FORWARD = '\033[1C'

    ESCAPE_CODES = [COLOR_BLUE, COLOR_GREEN, COLOR_YELLOW, COLOR_RED,
                    COLOR_DARKGREY, CONTROL_END, MOVE_BACK, MOVE_FORWARD]

    """
    Class to help applications to colorize their outputs for terminals.

    This will probe the current terminal and colorize ouput only if the
    stdout is in a tty or the terminal type is recognized.
    """

    def __init__(self):
        self.HEADER = self.COLOR_BLUE
        self.PASS = self.COLOR_GREEN
        self.SKIP = self.COLOR_YELLOW
        self.FAIL = self.COLOR_RED
        self.INTERRUPT = self.COLOR_RED
        self.ERROR = self.COLOR_RED
        self.WARN = self.COLOR_YELLOW
        self.PARTIAL = self.COLOR_YELLOW
        self.ENDC = self.CONTROL_END
        self.LOWLIGHT = self.COLOR_DARKGREY
        self.enabled = True
        if not is_colored_term():
            self.disable()

    def disable(self):
        """
        Disable colors from the strings output by this class.
        """
        self.enabled = False
        self.HEADER = ''
        self.PASS = ''
        self.SKIP = ''
        self.FAIL = ''
        self.INTERRUPT = ''
        self.ERROR = ''
        self.WARN = ''
        self.PARTIAL = ''
        self.ENDC = ''
        self.LOWLIGHT = ''
        self.MOVE_BACK = ''
        self.MOVE_FORWARD = ''

    def header_str(self, msg):
        """
        Print a header string (blue colored).

        If the output does not support colors, just return the original string.
        """
        return self.HEADER + msg + self.ENDC

    def fail_header_str(self, msg):
        """
        Print a fail header string (red colored).

        If the output does not support colors, just return the original string.
        """
        return self.FAIL + msg + self.ENDC

    def warn_header_str(self, msg):
        """
        Print a warning header string (yellow colored).

        If the output does not support colors, just return the original string.
        """
        return self.WARN + msg + self.ENDC

    def healthy_str(self, msg):
        """
        Print a healthy string (green colored).

        If the output does not support colors, just return the original string.
        """
        return self.PASS + msg + self.ENDC

    def partial_str(self, msg):
        """
        Print a string that denotes partial progress (yellow colored).

        If the output does not support colors, just return the original string.
        """
        return self.PARTIAL + msg + self.ENDC

    def pass_str(self):
        """
        Print a pass string (green colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.PASS + 'PASS' + self.ENDC

    def skip_str(self):
        """
        Print a skip string (yellow colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.SKIP + 'SKIP' + self.ENDC

    def fail_str(self):
        """
        Print a fail string (red colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.FAIL + 'FAIL' + self.ENDC

    def error_str(self):
        """
        Print a error string (red colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.ERROR + 'ERROR' + self.ENDC

    def interrupt_str(self):
        """
        Print an interrupt string (red colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.INTERRUPT + 'INTERRUPT' + self.ENDC

    def warn_str(self):
        """
        Print an warning string (yellow colored).

        If the output does not support colors, just return the original string.
        """
        return self.MOVE_BACK + self.WARN + 'WARN' + self.ENDC


term_support = TermSupport()


class LoggingFile(object):

    """
    File-like object that will receive messages pass them to logging.
    """

    def __init__(self, prefix='', level=logging.DEBUG,
                 logger=[logging.getLogger()]):
        """
        Constructor. Sets prefixes and which logger is going to be used.

        :param prefix - The prefix for each line logged by this object.
        """

        self._prefix = prefix
        self._level = level
        self._buffer = []
        if not isinstance(logger, list):
            logger = [logger]
        self._logger = logger

    def write(self, data):
        """"
        Writes data only if it constitutes a whole line. If it's not the case,
        store it in a buffer and wait until we have a complete line.
        :param data - Raw data (a string) that will be processed.
        """
        # splitlines() discards a trailing blank line, so use split() instead
        data_lines = data.split('\n')
        if len(data_lines) > 1:
            self._buffer.append(data_lines[0])
            self._flush_buffer()
        for line in data_lines[1:-1]:
            self._log_line(line)
        if data_lines[-1]:
            self._buffer.append(data_lines[-1])

    def writelines(self, lines):
        """"
        Writes itertable of lines

        :param lines: An iterable of strings that will be processed.
        """
        for data in lines:
            self.write(data)

    def _log_line(self, line):
        """
        Passes lines of output to the logging module.
        """
        for lg in self._logger:
            lg.log(self._level, self._prefix + line)

    def _flush_buffer(self):
        if self._buffer:
            self._log_line(''.join(self._buffer))
            self._buffer = []

    def flush(self):
        self._flush_buffer()

    def isatty(self):
        return False


class Throbber(object):

    """
    Produces a spinner used to notify progress in the application UI.
    """
    STEPS = ['-', '\\', '|', '/']
    # Only print a throbber when we're on a terminal
    if term_support.enabled:
        MOVES = [term_support.MOVE_BACK + STEPS[0],
                 term_support.MOVE_BACK + STEPS[1],
                 term_support.MOVE_BACK + STEPS[2],
                 term_support.MOVE_BACK + STEPS[3]]
    else:
        MOVES = ['', '', '', '']

    def __init__(self):
        self.position = 0

    def _update_position(self):
        if self.position == (len(self.MOVES) - 1):
            self.position = 0
        else:
            self.position += 1

    def render(self):
        result = self.MOVES[self.position]
        self._update_position()
        return result
