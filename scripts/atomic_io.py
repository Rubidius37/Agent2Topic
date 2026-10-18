import json
import os
import stat
import tempfile


class StateFileError(RuntimeError):
    pass


def filesystem_path(path):
    """Use extended Windows paths for I/O without changing stored relative paths."""
    path = os.fspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _default_new_file_mode():
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def atomic_write_text(path, content, encoding="utf-8"):
    raw_abs = os.path.abspath(path)
    target_abs = os.path.realpath(raw_abs) if os.path.islink(raw_abs) else raw_abs
    absolute_path = filesystem_path(target_abs)
    directory = os.path.dirname(absolute_path)
    os.makedirs(directory, exist_ok=True)

    existing_mode = None
    try:
        existing_mode = stat.S_IMODE(os.stat(absolute_path).st_mode)
    except OSError:
        existing_mode = None

    descriptor, temp_path = tempfile.mkstemp(
        prefix=f".research-notes-{os.path.basename(target_abs)}-",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(
            descriptor,
            "w",
            encoding=encoding,
            newline="",
        ) as file_obj:
            file_obj.write(content)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        try:
            target_mode = existing_mode if existing_mode is not None else _default_new_file_mode()
            os.chmod(temp_path, target_mode)
        except OSError:
            pass
        os.replace(temp_path, absolute_path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path, value, indent=2, ensure_ascii=False):
    try:
        content = json.dumps(
            value,
            indent=indent,
            ensure_ascii=ensure_ascii,
        )
    except (TypeError, ValueError) as exc:
        raise StateFileError(f"Could not serialize JSON state for {path}: {exc}") from exc
    atomic_write_text(path, content)


def load_json_state(path, missing_default, expected_type=dict, label="state file"):
    if not os.path.exists(path):
        return missing_default
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            value = json.load(file_obj)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StateFileError(f"Could not read {label} {path}: {exc}") from exc
    if expected_type is not None and not isinstance(value, expected_type):
        raise StateFileError(
            f"Invalid {label} {path}: expected {expected_type.__name__}, "
            f"got {type(value).__name__}."
        )
    return value
