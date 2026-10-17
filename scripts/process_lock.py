import json
import os
import socket
import sys
from datetime import datetime


LOCK_BUSY_EXIT_CODE = 3
LOCK_PARENT_PID_ENV = "LOG2TOPIC_LOCK_PARENT_PID"
LOCK_RELATIVE_PATH = os.path.join("scripts", ".runtime", "automation.lock")


class ProcessLockUnavailable(RuntimeError):
    def __init__(self, lock_path, owner=None):
        self.lock_path = lock_path
        self.owner = owner if isinstance(owner, dict) else {}
        super().__init__(self._build_message())

    def _build_message(self):
        lines = ["Another Log2Topic automation is already running."]
        labels = (
            ("operation", "Operation"),
            ("pid", "PID"),
            ("started_at", "Started"),
            ("hostname", "Host"),
            ("command", "Command"),
        )
        for key, label in labels:
            value = self.owner.get(key)
            if value not in (None, ""):
                lines.append(f"{label}: {value}")
        lines.append("No files or remote pages were changed by this run.")
        return "\n".join(lines)


def get_vault_lock_path(vault_dir):
    return os.path.join(os.path.abspath(vault_dir), LOCK_RELATIVE_PATH)


def command_text(script_path, argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    values = [os.path.basename(script_path), *[str(value) for value in arguments]]
    return " ".join(values)[:2000]


def lock_is_delegated_by_parent():
    expected_parent = os.environ.get(LOCK_PARENT_PID_ENV, "").strip()
    return expected_parent.isdigit() and int(expected_parent) == os.getppid()


def delegated_lock_environment(base_env=None):
    env = dict(os.environ if base_env is None else base_env)
    env[LOCK_PARENT_PID_ENV] = str(os.getpid())
    return env


def _read_owner(lock_path):
    try:
        with open(lock_path, "rb") as file_obj:
            file_obj.seek(1)
            payload = file_obj.read().decode("utf-8")
        value = json.loads(payload)
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _lock_file(file_obj):
    file_obj.flush()
    file_obj.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file_obj.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(file_obj):
    file_obj.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file_obj.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(file_obj.fileno(), fcntl.LOCK_UN)


class VaultProcessLock:
    def __init__(self, vault_dir, operation, command=""):
        self.vault_dir = os.path.abspath(vault_dir)
        self.operation = str(operation)
        self.command = str(command)
        self.lock_path = get_vault_lock_path(self.vault_dir)
        self._file_obj = None

    def _open_lock_file(self):
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        try:
            file_obj = open(self.lock_path, "x+b")
            file_obj.write(b"\0")
            file_obj.flush()
            return file_obj
        except FileExistsError:
            file_obj = open(self.lock_path, "r+b")
            file_obj.seek(0, os.SEEK_END)
            if file_obj.tell() == 0:
                file_obj.write(b"\0")
                file_obj.flush()
            return file_obj

    def acquire(self):
        if self._file_obj is not None:
            return self

        file_obj = self._open_lock_file()
        try:
            _lock_file(file_obj)
        except (OSError, BlockingIOError):
            file_obj.close()
            raise ProcessLockUnavailable(self.lock_path, _read_owner(self.lock_path))

        owner = {
            "pid": os.getpid(),
            "operation": self.operation,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "hostname": socket.gethostname(),
            "command": self.command,
        }
        encoded = json.dumps(owner, indent=2, ensure_ascii=False).encode("utf-8")
        try:
            file_obj.seek(1)
            file_obj.truncate()
            file_obj.write(encoded)
            file_obj.flush()
            os.fsync(file_obj.fileno())
        except Exception:
            try:
                _unlock_file(file_obj)
            finally:
                file_obj.close()
            raise

        self._file_obj = file_obj
        return self

    def release(self):
        if self._file_obj is None:
            return
        file_obj = self._file_obj
        self._file_obj = None
        try:
            _unlock_file(file_obj)
        finally:
            file_obj.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()
        return False


def print_lock_error(exc):
    print(f"[BUSY] {exc}")
