"""Small JSON-file stores kept under data/ (a Railway Volume in production)."""
from __future__ import annotations

import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

PathLike = Union[str, Path]


def ensure_private_dir(path: PathLike) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


class JsonFile:
    """A JSON document written atomically (tmp file with mode 0600, then os.replace)."""

    def __init__(self, path: PathLike, default: Callable[[], Any]):
        self.path = Path(path)
        self._default = default

    def read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return self._default()

    def write(self, data: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class HistoryStore:
    """Per business-connection and per-chat conversation history."""

    def __init__(self, path: PathLike, limit: int):
        self._file = JsonFile(path, dict)
        self.limit = limit
        data = self._file.read()
        self._data: Dict[str, List[dict]] = data if isinstance(data, dict) else {}

    @staticmethod
    def key(connection_id: str, chat_id: int) -> str:
        return f"{connection_id}:{chat_id}"

    def get(self, connection_id: str, chat_id: int) -> List[dict]:
        return [dict(item) for item in self._data.get(self.key(connection_id, chat_id), [])]

    def add(
        self,
        connection_id: str,
        chat_id: int,
        role: str,
        name: str,
        text: str,
        lang: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        items = self._data.setdefault(self.key(connection_id, chat_id), [])
        items.append(
            {"role": role, "name": name, "text": text, "lang": lang, "ts": ts if ts is not None else time.time()}
        )
        del items[: max(0, len(items) - self.limit)]
        self._file.write(self._data)

    def last_language(self, connection_id: str, chat_id: int) -> Optional[str]:
        for item in reversed(self._data.get(self.key(connection_id, chat_id), [])):
            if item.get("role") == "other" and item.get("lang"):
                return item["lang"]
        return None

    def chat_count(self) -> int:
        return len(self._data)


class SeenMessages:
    """Remembers processed message keys across restarts to drop duplicates."""

    def __init__(self, path: PathLike, capacity: int = 5000):
        self._file = JsonFile(path, list)
        self.capacity = capacity
        data = self._file.read()
        self._keys: "OrderedDict[str, None]" = OrderedDict(
            (str(k), None) for k in (data if isinstance(data, list) else [])
        )

    def first_time(self, key: str) -> bool:
        if key in self._keys:
            return False
        self._keys[key] = None
        while len(self._keys) > self.capacity:
            self._keys.popitem(last=False)
        self._file.write(list(self._keys))
        return True


class RuntimeState:
    """Pause flag and usage-limit deadline, persisted across restarts."""

    def __init__(self, path: PathLike):
        self._file = JsonFile(path, dict)
        data = self._file.read()
        self._data: Dict[str, Any] = data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self._file.write(self._data)

    @property
    def paused(self) -> bool:
        return bool(self._data.get("paused", False))

    @paused.setter
    def paused(self, value: bool) -> None:
        self._data["paused"] = bool(value)
        self._save()

    @property
    def limit_until(self) -> Optional[float]:
        value = self._data.get("limit_until")
        return float(value) if value else None

    @limit_until.setter
    def limit_until(self, value: Optional[float]) -> None:
        self._data["limit_until"] = value
        self._save()
