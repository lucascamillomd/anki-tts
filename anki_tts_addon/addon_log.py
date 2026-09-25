"""Rotating log file for the add-on (user_files/anki_tts.log, 1 MB x 2)."""

import logging
import logging.handlers
from pathlib import Path
from typing import Optional


LOG_FILE_NAME = "anki_tts.log"
MAX_BYTES = 1024 * 1024
BACKUP_COUNT = 2
_HANDLER_MARK = "_anki_tts_file_handler"

_log_path: Optional[Path] = None


def setup(logger_name: str, user_files_dir) -> Optional[Path]:
    """Attach (or replace) the rotating file handler; never raises."""
    global _log_path
    logger = logging.getLogger(logger_name)
    try:
        path = Path(user_files_dir) / LOG_FILE_NAME
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARK, False):
                logger.removeHandler(handler)
                handler.close()
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=True,
        )
        setattr(handler, _HANDLER_MARK, True)
        handler.setLevel(logging.INFO)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
            )
        )
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
        _log_path = path
        return path
    except Exception:
        logging.getLogger(__name__).debug("Could not set up log file", exc_info=True)
        return None


def log_path() -> Optional[Path]:
    return _log_path


def remove(logger_name: str) -> None:
    """Detach and close our file handler (tests; add-on reloads)."""
    global _log_path
    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
    _log_path = None
