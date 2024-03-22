import os
import sys
import atexit
import argparse
import multiprocessing as mp
import logging as stdlogging
from typing import NamedTuple
from statemachine import StateMachine, State
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

import bittensor.config
from bittensor.btlogging.format import BtStreamFormatter, BtFileFormatter
from bittensor.btlogging.helpers import all_loggers
from bittensor.btlogging.defines import (
    TRACE_LOG_FORMAT,
    DATE_FORMAT,
    BITTENSOR_LOGGER_NAME,
    DEFAULT_LOG_FILE_NAME,
    DEFAULT_MAX_ROTATING_LOG_FILE_SIZE,
    DEFAULT_LOG_BACKUP_COUNT,
)


class LoggingConfig(NamedTuple):
    debug: bool
    trace: bool
    record_log: bool
    logging_dir: str


class LoggingMachine(StateMachine):
    """
    Handles logger states for bittensor and 3rd party libraries
    """

    Default = State(initial=True)
    Debug = State()
    Trace = State()
    Disabled = State()

    enable_default = (
        Debug.to(Default)
        | Trace.to(Default)
        | Disabled.to(Default)
        | Default.to(Default)
    )

    enable_trace = (
        Default.to(Trace) | Debug.to(Trace) | Disabled.to(Trace) | Trace.to(Trace)
    )

    enable_debug = (
        Default.to(Debug) | Trace.to(Debug) | Disabled.to(Debug) | Debug.to(Debug)
    )

    disable_trace = Trace.to(Default)

    disable_debug = Debug.to(Default)

    disable_logging = (
        Trace.to(Disabled)
        | Debug.to(Disabled)
        | Default.to(Disabled)
        | Disabled.to(Disabled)
    )

    def __init__(self, config: bittensor.config, name: str = BITTENSOR_LOGGER_NAME):
        # basics
        super(LoggingMachine, self).__init__()
        self._queue = mp.Queue(-1)
        self._name = name
        self._config = config

        # Formatters
        #
        # In the future, this may be expanded to a dictionary mapping handler
        # types to their respective formatters.
        self._stream_formatter = BtStreamFormatter()
        self._file_formatter = BtFileFormatter(TRACE_LOG_FORMAT, DATE_FORMAT)

        # start with handlers for the QueueListener.
        #
        # In the future, we may want to add options to introduce other handlers
        # for things like log aggregation by external services.
        self._handlers = self._configure_handlers(config)

        # configure and start the queue listener
        self._listener = self._create_and_start_listener(self._handlers)

        # set up all the loggers
        self._logger = self._initialize_bt_logger(name, config)
        self._initialize_external_loggers(config)

    def _configure_handlers(self, config) -> list[stdlogging.Handler]:
        handlers = list()

        # stream handler, a given
        stream_handler = stdlogging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(self._stream_formatter)
        handlers.append(stream_handler)

        # file handler, maybe
        if config.record_log and config.logging_dir:
            logfile = os.path.abspath(
                os.path.join(config.logging_dir, DEFAULT_LOG_FILE_NAME)
            )
            file_handler = self._create_file_handler(logfile)
            handlers.append(file_handler)
        return handlers

    def get_config(self):
        return self._config

    def set_config(self, config):
        """
        Set config after initialization, if desired.
        """
        self._config = config
        if config.logging_dir and config.record_log:
            logfile = os.path.abspath(
                os.path.join(config.logging_dir, DEFAULT_LOG_FILE_NAME)
            )
            self._enable_file_logging(logfile)
        if config.trace:
            self.enable_trace()
        elif config.debug:
            self.enable_debug()

    def _create_and_start_listener(self, handlers):
        """
        A listener to receive and publish log records.

        This listener receives records from a queue populated by the main bittensor
        logger, as well as 3rd party loggers.
        """

        listener = QueueListener(self._queue, *handlers, respect_handler_level=True)
        listener.start()
        atexit.register(listener.stop)
        return listener

    def get_queue(self):
        """
        Get the queue the QueueListener is publishing from.

        To set up logging in a separate process, a QueueHandler must
        be added to all the desired loggers.
        """
        return self._queue

    def _initialize_bt_logger(self, name, config):
        """
        Initialize logging for bittensor.

        Since the initial state is Default, logging level for the module logger
        is INFO, and all third-party loggers are silenced. Subsequent state
        transitions will handle all logger outputs.
        """
        logger = stdlogging.getLogger(name)
        queue_handler = QueueHandler(self._queue)
        logger.addHandler(queue_handler)
        return logger

    def _create_file_handler(self, logfile: str):
        file_handler = RotatingFileHandler(
            logfile,
            maxBytes=DEFAULT_MAX_ROTATING_LOG_FILE_SIZE,
            backupCount=DEFAULT_LOG_BACKUP_COUNT,
        )
        file_handler.setFormatter(self._file_formatter)
        file_handler.setLevel(stdlogging.TRACE)
        return file_handler

    def _initialize_external_loggers(self, config):
        # remove all handlers
        for logger in all_loggers():
            if logger.name == self._name:
                continue
            for handler in logger.handlers:
                logger.removeHandler(handler)
            queue_handler = QueueHandler(self._queue)
            logger.addHandler(queue_handler)
            logger.setLevel(stdlogging.CRITICAL)

    def _enable_file_logging(self, logfile: str):
        # preserve idempotency; do not create extra filehandlers
        # if one already exists
        if any(
            [isinstance(handler, RotatingFileHandler) for handler in self._handlers]
        ):
            return
        file_handler = self._create_file_handler(logfile)
        self._handlers.append(file_handler)
        self._listener.handlers = tuple(self._handlers)

    # state transitions
    def before_transition(self, event, state):
        self._listener.stop()

    def after_transition(self, event, state):
        self._listener.start()

    # Default Logging
    def before_enable_default(self):
        self._logger.info(f"Enabling default logging.")
        self._logger.setLevel(stdlogging.INFO)
        for logger in all_loggers():
            if logger.name == self._name:
                continue
            logger.setLevel(stdlogging.CRITICAL)

    def after_enable_default(self):
        pass

    # Trace
    def before_enable_trace(self):
        self._logger.info("Enabling trace.")
        self._stream_formatter.set_trace(True)
        for logger in all_loggers():
            logger.setLevel(stdlogging.TRACE)

    def after_enable_trace(self):
        self._logger.info("Trace enabled.")

    def before_disable_trace(self):
        self._logger.info(f"Disabling trace.")
        self._stream_formatter.set_trace(False)
        self.enable_default()

    def after_disable_trace(self):
        self._logger.info("Trace disabled.")

    # Debug
    def before_enable_debug(self):
        self._logger.info("Enabling debug.")
        self._stream_formatter.set_trace(True)
        for logger in all_loggers():
            logger.setLevel(stdlogging.DEBUG)

    def after_enable_debug(self):
        self._logger.info("Debug enabled.")

    def before_disable_debug(self):
        self._logger.info("Disabling debug.")
        self._stream_formatter.set_trace(False)
        self.enable_default()

    def after_disable_debug(self):
        self._logger.info("Debug disabled.")

    # Disable Logging
    def before_disable_logging(self):
        self._logger.info("Disabling logging.")
        self._stream_formatter.set_trace(False)

        for logger in all_loggers():
            logger.setLevel(stdlogging.CRITICAL)

    # Required API
    # support log commands for API backwards compatibility
    @property
    def __trace_on__(self):
        return self.current_state_value == "Trace"

    def trace(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.trace(msg, *args, **kwargs)

    def debug(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.info(msg, *args, **kwargs)

    def success(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.success(msg, *args, **kwargs)

    def warning(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.error(msg, *args, **kwargs)

    def critical(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.critical(msg, *args, **kwargs)

    def exception(self, msg="", prefix="", sufix="", *args, **kwargs):
        msg = f"{prefix} - {msg} - {sufix}"
        self._logger.exception(msg, *args, **kwargs)

    def on(self):
        self._logger.info("Logging enabled.")
        self.enable_default()

    def off(self):
        self.disable_logging()

    def set_debug(self, on: bool = True):
        if on and not self.current_state_value == "Debug":
            self.enable_debug()
        elif not on:
            if self.current_state_value == "Debug":
                self.disable_debug()

    def set_trace(self, on: bool = True):
        if on and not self.current_state_value == "Trace":
            self.enable_trace()
        elif not on:
            if self.current_state_value == "Trace":
                self.disable_trace()

    def get_level(self):
        return self._logger.level
    
    def check_config(self, config: bittensor.config):
        assert config.logging

    def help(self):
        pass

    @classmethod
    def add_args(cls, parser: argparse.ArgumentParser, prefix: str = None):
        """Accept specific arguments fro parser"""
        prefix_str = "" if prefix == None else prefix + "."
        try:
            default_logging_debug = os.getenv("BT_LOGGING_DEBUG") or False
            default_logging_trace = os.getenv("BT_LOGGING_TRACE") or False
            default_logging_record_log = os.getenv("BT_LOGGING_RECORD_LOG") or False
            default_logging_logging_dir = (
                os.getenv("BT_LOGGING_LOGGING_DIR") or "~/.bittensor/miners"
            )
            parser.add_argument(
                "--" + prefix_str + "logging.debug",
                action="store_true",
                help="""Turn on bittensor debugging information""",
                default=default_logging_debug,
            )
            parser.add_argument(
                "--" + prefix_str + "logging.trace",
                action="store_true",
                help="""Turn on bittensor trace level information""",
                default=default_logging_trace,
            )
            parser.add_argument(
                "--" + prefix_str + "logging.record_log",
                action="store_true",
                help="""Turns on logging to file.""",
                default=default_logging_record_log,
            )
            parser.add_argument(
                "--" + prefix_str + "logging.logging_dir",
                type=str,
                help="Logging default root directory.",
                default=default_logging_logging_dir,
            )
        except argparse.ArgumentError:
            # re-parsing arguments.
            pass

    @classmethod
    def config(cls):
        """Get config from the argument parser.

        Return:
            bittensor.config object
        """
        parser = argparse.ArgumentParser()
        cls.add_args(parser)
        return bittensor.config(parser, args=[])
