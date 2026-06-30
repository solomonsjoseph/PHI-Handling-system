"""Canonical centralized logging system for RePORT AI Portal.

This module provides the single supported logging boundary for the
single-study, privacy-first, local-first runtime. It exposes:

- one application logger rooted at ``report_ai_portal``
- a custom ``SUCCESS`` log level between ``INFO`` and ``WARNING``
- rotating file handlers plus filtered console output
- convenience functions, timing/error decorators, and verbose tree logging
- log cleanup helpers for retention management

Design rules:
- Logger instances are obtained via ``logging.getLogger(...)`` and cached.
- Initialization is thread-safe and idempotent.
- File logging supports rotation via ``RotatingFileHandler``.
"""

from __future__ import annotations

import functools
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
UTC = timezone.utc  # py3.9 compat
from pathlib import Path
from typing import Any

__all__ = [
    "MODULE_CATEGORY_MAP",
    "SUCCESS",
    "CustomFormatter",
    "JSONFormatter",
    "VerboseLogger",
    "cleanup_old_logs",
    "critical",
    "debug",
    "error",
    "exception",
    "get_log_file_path",
    "get_logger",
    "get_verbose_logger",
    "info",
    "log_errors",
    "log_execution_time",
    "log_time",
    "reset_logging",
    "setup_logger",
    "setup_logging",
    "success",
    "warning",
]

SUCCESS = 25
logging.addLevelName(SUCCESS, "SUCCESS")

_logger: logging.Logger | None = None
_log_file_path: str | None = None
_logger_lock = threading.Lock()

_verbose_logger: VerboseLogger | None = None

MODULE_CATEGORY_MAP = {
    "scripts.extraction.extract_dataset": "data_cleaning_and_processing",
    "scripts.extraction.dataset_pipeline": "data_cleaning_and_processing",
    "scripts.extraction.load_dictionary": "data_cleaning_and_processing",
    "scripts.extraction.dedup": "data_cleaning_and_processing",
    "scripts.extraction.dataset_cleanup": "data_cleaning_and_processing",
    "scripts.security.secure_env": "security",
    "scripts.ai_assistant.agent_graph": "AI Assistant/agent",
    "scripts.ai_assistant.agent_tools": "AI Assistant/agent",
    "scripts.ai_assistant.tool_cache": "AI Assistant/agent",
    "scripts.ai_assistant.cli": "AI Assistant/cli",
    "scripts.ai_assistant.web_ui": "AI Assistant/web",
    "scripts.utils.telemetry": "telemetry",
    "scripts.utils.step_cache": "caching",
    "__main__": "main",
    "main": "main",
}


def _success_method(self: logging.Logger, msg: str, *args: Any, **kwargs: Any) -> None:
    """Support ``logger.success(...)`` on standard logger instances."""
    if self.isEnabledFor(SUCCESS):
        self.log(SUCCESS, msg, *args, **kwargs)


logging.Logger.success = _success_method  # type: ignore[attr-defined]


class CustomFormatter(logging.Formatter):
    """Standard text formatter with explicit SUCCESS label support."""

    def format(self, record: logging.LogRecord) -> str:
        if record.levelno == SUCCESS:
            record.levelname = "SUCCESS"
        return super().format(record)


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured audit and machine-readable logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
            "thread_id": record.thread,
            "thread_name": record.threadName,
            "process_id": record.process,
            "process_name": record.processName,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload["extra"] = extra
        return json.dumps(payload, ensure_ascii=False)


class InfoAndAboveFilter(logging.Filter):
    """Allow INFO and above, including the custom SUCCESS level."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.INFO


class SuccessOrWarningFilter(logging.Filter):
    """Allow SUCCESS and all WARNING+ records."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == SUCCESS or record.levelno >= logging.WARNING


class VerboseLogger:
    """Hierarchical helper for DEBUG-mode tree logging."""

    def __init__(self, logger_module: types.ModuleType) -> None:
        self.log = logger_module
        self._state = threading.local()

    def _indent(self) -> int:
        value = getattr(self._state, "indent", 0)
        return value if isinstance(value, int) and value > 0 else 0

    def _set_indent(self, value: int) -> None:
        self._state.indent = max(0, value)

    def __call__(self, message: str) -> None:
        if self._is_verbose():
            self.log.debug(message)

    def _is_verbose(self) -> bool:
        try:
            root = get_logger()
            return root.isEnabledFor(logging.DEBUG)
        except Exception:
            return False

    def _log_tree(self, prefix: str, message: str) -> None:
        if not self._is_verbose():
            return
        with suppress(Exception):
            indent = "  " * self._indent()
            self.log.debug("%s%s%s", indent, prefix, message)

    @contextmanager
    def _scope(self, prefix: str, header: str, footer: str | None = None):
        self._log_tree(prefix, header)
        self._set_indent(self._indent() + 1)
        try:
            yield
        finally:
            self._set_indent(self._indent() - 1)
            if footer:
                self._log_tree("└─ ", footer)

    def file_processing(self, filename: str, total_records: int | None = None):
        header = f"Processing: {filename}"
        if total_records is not None:
            header += f" ({total_records} records)"
        return self._scope("├─ ", header, "✓ Complete")

    def step(self, step_name: str):
        return self._scope("├─ ", step_name)

    def detail(self, message: str) -> None:
        self._log_tree("│  ", message)

    def metric(self, label: str, value: Any) -> None:
        self._log_tree("├─ ", f"{label}: {value}")

    def timing(self, operation: str, seconds: float) -> None:
        self._log_tree("├─ ", f"⏱ {operation}: {seconds:.2f}s")

    def items_list(self, label: str, items: Sequence[Any], max_show: int = 5) -> None:
        if not self._is_verbose():
            return
        shown = [str(item) for item in items[:max_show]]
        if len(items) > max_show:
            self.detail(f"{label}: {', '.join(shown)} ... (+{len(items) - max_show} more)")
        else:
            self.detail(f"{label}: {', '.join(shown)}")


def _resolve_log_level(log_level: str | None) -> int:
    """Resolve a user/environment log-level string to a numeric level."""
    configured = (log_level or os.getenv("LOG_LEVEL") or "INFO").upper().strip()
    if not hasattr(logging, configured):
        raise ValueError(f"Invalid log level: {configured}")
    value = getattr(logging, configured)
    if not isinstance(value, int):
        raise ValueError(f"Invalid log level: {configured}")
    return value


def _get_log_category(module_name: str) -> str:
    """Map a module name to its log-category directory."""
    if module_name in MODULE_CATEGORY_MAP:
        return MODULE_CATEGORY_MAP[module_name]
    for module_prefix, category in MODULE_CATEGORY_MAP.items():
        if module_name.startswith(module_prefix + "."):
            return category
    return "main"


def _get_log_directory(
    category: str,
    *,
    base_dir: Path | None = None,
    use_category: bool = True,
) -> Path:
    """Return the directory where log files should be written.

    Does NOT create the directory — ``_LazyDirRotatingFileHandler`` creates
    it on first emit, so importing a module that calls ``get_logger()``
    touches the filesystem only when something is actually logged.
    """
    if base_dir is None:
        logs_root = os.getenv("LOG_DIR", ".logs")
        base_dir = Path(logs_root) / "RePORT AI Portal"
    return base_dir / category if use_category else base_dir


class _LazyDirRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that creates its parent directory on first open."""

    def _open(self):
        Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        return super()._open()


def _append_log_path(msg: str, include_log_path: bool) -> str:
    """Append the current log-file path to a user-facing message when requested."""
    if include_log_path and _log_file_path:
        return f"{msg}\nFor more details, check the log file at: {_log_file_path}"
    return msg


def setup_logging(
    module_name: str = "__main__",
    log_level: str | None = None,
    simple_mode: bool = False,
    verbose: bool = False,
    json_format: bool = False,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
) -> logging.Logger:
    """Create and configure the singleton application logger."""
    global _logger, _log_file_path

    if _logger is not None:
        return _logger

    with _logger_lock:
        if _logger is not None:
            return _logger

        numeric_level = _resolve_log_level(log_level)
        resolved_verbose = verbose or os.getenv("LOG_VERBOSE", "").lower() == "true"
        resolved_json = json_format or os.getenv("LOG_FORMAT", "").lower() == "json"

        logger = logging.getLogger("report_ai_portal")
        logger.setLevel(numeric_level)
        logger.propagate = False
        logger.handlers.clear()

        category = _get_log_category(module_name)
        log_dir = _get_log_directory(category, use_category=resolved_verbose)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        if resolved_verbose:
            module_simple_name = (
                module_name.split(".")[-1] if module_name != "__main__" else "report_ai_portal_main"
            )
            log_file = log_dir / f"{module_simple_name}_{timestamp}.log"
        else:
            log_file = log_dir / f"report_ai_portal_{timestamp}.log"
        _log_file_path = str(log_file)

        file_handler = _LazyDirRotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(numeric_level)

        if resolved_json:
            file_handler.setFormatter(JSONFormatter())
        else:
            if numeric_level <= logging.DEBUG:
                file_format = (
                    "%(asctime)s - [PID:%(process)d TID:%(thread)d] - %(name)s - %(levelname)s - "
                    "[%(filename)s:%(lineno)d:%(funcName)s] - %(message)s"
                )
            else:
                file_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            file_handler.setFormatter(CustomFormatter(file_format))

        console_handler = logging.StreamHandler(sys.stdout)
        if resolved_verbose:
            console_handler.setLevel(numeric_level)
            console_handler.setFormatter(
                CustomFormatter("%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S")
            )
        elif simple_mode:
            console_handler.setLevel(logging.INFO)
            console_handler.addFilter(InfoAndAboveFilter())
            console_handler.setFormatter(CustomFormatter("%(levelname)s: %(message)s"))
        else:
            console_handler.setLevel(logging.WARNING)
            console_handler.addFilter(SuccessOrWarningFilter())
            console_handler.setFormatter(CustomFormatter("%(levelname)s: %(message)s"))

        # Late-bind PHI redaction: if install_phi_redactor() already ran, its
        # filter sits on the root logger. Logger-level filters never apply to
        # records propagated from child loggers, so attach it to our handlers
        # (handler filters see every record that reaches them). The reverse
        # order (setup first, redactor later) is covered in install_phi_redactor.
        for root_filter in logging.getLogger().filters:
            if type(root_filter).__name__ == "PHIRedactingFilter":
                file_handler.addFilter(root_filter)
                console_handler.addFilter(root_filter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        # DEBUG so that under the default INFO level no record is emitted at
        # setup time — keeps get_logger() free of import-time file creation.
        logger.debug(
            "Logging initialized | mode=%s | category=%s | file=%s",
            "verbose" if resolved_verbose else "default",
            category,
            log_file,
        )

        _logger = logger
        return logger


def reset_logging() -> None:
    """Reset main logger state. Primarily for tests."""
    global _logger, _log_file_path, _verbose_logger

    if _logger is not None:
        for handler in _logger.handlers[:]:
            handler.close()
            _logger.removeHandler(handler)
        _logger = None
        _log_file_path = None

    _verbose_logger = None


def setup_logger(
    name: str = "report_ai_portal",
    log_level: int = logging.INFO,
    simple_mode: bool = False,
    verbose: bool = False,
) -> logging.Logger:
    """Legacy compatibility wrapper around ``setup_logging``."""
    level_name = logging.getLevelName(log_level)
    module_name = name if name != "report_ai_portal" else "__main__"
    return setup_logging(
        module_name=module_name,
        log_level=level_name,
        simple_mode=simple_mode,
        verbose=verbose,
    )


def get_logger(name: str | None = None) -> logging.Logger:
    """Return the root application logger or a child logger."""
    root = _logger if _logger is not None else setup_logging()
    if name is None:
        return root
    if name.startswith("report_ai_portal"):
        return logging.getLogger(name)
    return root.getChild(name)


def get_log_file_path() -> str | None:
    """Return the current main log-file path if initialized."""
    return _log_file_path


def cleanup_old_logs(
    max_age_days: int | None = None,
    max_files: int | None = None,
    log_dir: Path | None = None,
    dry_run: bool = False,
    recursive: bool = True,
    pattern: str = "*.log",
) -> dict[str, Any]:
    """Delete old log files according to age and/or count criteria."""
    if max_age_days is None and max_files is None:
        raise ValueError("At least one of max_age_days or max_files must be specified")
    if max_age_days is not None and max_age_days <= 0:
        raise ValueError("max_age_days must be positive")
    if max_files is not None and max_files < 0:
        raise ValueError("max_files must be non-negative")

    if log_dir is None:
        logs_root = os.getenv("LOG_DIR", ".logs")
        log_dir = Path(logs_root) / "RePORT AI Portal"
    else:
        log_dir = Path(log_dir)

    if not log_dir.exists():
        return {
            "files_scanned": 0,
            "files_deleted": 0,
            "files_skipped": 0,
            "bytes_freed": 0,
            "dry_run": dry_run,
            "deleted_files": [],
        }

    logger = get_logger(__name__)
    active_log_file = _log_file_path
    log_files = list(log_dir.rglob(pattern)) if recursive else list(log_dir.glob(pattern))
    log_files = [f for f in log_files if f.is_file()]

    files_scanned = len(log_files)
    files_deleted = 0
    files_skipped = 0
    bytes_freed = 0
    deleted_files: list[str] = []

    cutoff_time = None
    if max_age_days is not None:
        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)

    log_files_sorted = sorted(log_files, key=lambda f: f.stat().st_mtime, reverse=True)

    for idx, log_file in enumerate(log_files_sorted):
        try:
            if active_log_file and str(log_file.resolve()) == str(Path(active_log_file).resolve()):
                files_skipped += 1
                continue

            stat = log_file.stat()
            should_delete = False
            reasons: list[str] = []
            if cutoff_time is not None and stat.st_mtime < cutoff_time:
                should_delete = True
                age_days = (time.time() - stat.st_mtime) / (24 * 60 * 60)
                reasons.append(f"older than {max_age_days} days (age: {age_days:.1f} days)")
            if max_files is not None and idx >= max_files:
                should_delete = True
                reasons.append(f"beyond top {max_files} recent files (rank: {idx + 1})")
            if not should_delete:
                continue

            if dry_run:
                files_deleted += 1
                bytes_freed += stat.st_size
                deleted_files.append(str(log_file))
                continue

            log_file.unlink()
            files_deleted += 1
            bytes_freed += stat.st_size
            deleted_files.append(str(log_file))
        except Exception:
            files_skipped += 1
            continue

    logger.info(
        "Log cleanup completed | scanned=%d | deleted=%d | skipped=%d | bytes_freed=%d | dry_run=%s",
        files_scanned,
        files_deleted,
        files_skipped,
        bytes_freed,
        dry_run,
    )
    return {
        "files_scanned": files_scanned,
        "files_deleted": files_deleted,
        "files_skipped": files_skipped,
        "bytes_freed": bytes_freed,
        "dry_run": dry_run,
        "deleted_files": deleted_files,
    }


def debug(msg: str, *args: Any, **kwargs: Any) -> None:
    get_logger().debug(msg, *args, **kwargs)


def info(msg: str, *args: Any, **kwargs: Any) -> None:
    get_logger().info(msg, *args, **kwargs)


def warning(msg: str, *args: Any, include_log_path: bool = False, **kwargs: Any) -> None:
    get_logger().warning(_append_log_path(msg, include_log_path), *args, **kwargs)


def error(msg: str, *args: Any, include_log_path: bool = True, **kwargs: Any) -> None:
    get_logger().error(_append_log_path(msg, include_log_path), *args, **kwargs)


def critical(msg: str, *args: Any, include_log_path: bool = True, **kwargs: Any) -> None:
    get_logger().critical(_append_log_path(msg, include_log_path), *args, **kwargs)


def success(msg: str, *args: Any, **kwargs: Any) -> None:
    get_logger().log(SUCCESS, msg, *args, **kwargs)


def exception(msg: str, *args: Any, include_log_path: bool = True, **kwargs: Any) -> None:
    kwargs.setdefault("exc_info", True)
    get_logger().error(_append_log_path(msg, include_log_path), *args, **kwargs)


def log_errors(logger_name: str | None = None, reraise: bool = True):
    """Decorator that logs exceptions with stack traces and optional re-raise."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any):
            logger = get_logger(logger_name or func.__module__)
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                logger.exception("Exception in %s: %s", func.__name__, exc)
                if reraise:
                    raise
                return None

        return wrapper

    return decorator


def log_time(logger_name: str | None = None, level: int = logging.INFO):
    """Decorator that logs function execution time."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any):
            logger = get_logger(logger_name or func.__module__)
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                elapsed = time.time() - start_time
                logger.log(level, "%s completed in %.2fs", func.__name__, elapsed)
                return result
            except Exception as exc:
                elapsed = time.time() - start_time
                logger.exception("%s failed after %.2fs: %s", func.__name__, elapsed, exc)
                raise

        return wrapper

    return decorator


@contextmanager
def log_execution_time(operation_name: str, logger_name: str | None = None):
    """Context manager that logs execution time for an arbitrary block."""
    logger = get_logger(logger_name)
    start_time = time.time()
    try:
        yield
        logger.info("%s completed in %.2fs", operation_name, time.time() - start_time)
    except Exception as exc:
        logger.exception(
            "%s failed after %.2fs: %s",
            operation_name,
            time.time() - start_time,
            exc,
        )
        raise


def get_verbose_logger() -> VerboseLogger:
    """Return the singleton verbose tree logger helper."""
    global _verbose_logger
    if _verbose_logger is None:
        _verbose_logger = VerboseLogger(sys.modules[__name__])
    return _verbose_logger
