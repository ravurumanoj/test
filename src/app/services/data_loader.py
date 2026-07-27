
"""JSON data loading helpers for tool-backed retrieval."""

from __future__ import annotations

import codecs
import json
import logging
from pathlib import Path
from typing import Any

from app.errors import DataAccessError

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"


def _cp1252_fallback(error: UnicodeDecodeError) -> tuple[str, int]:
    """Decode bytes that are invalid UTF-8 as Windows-1252 instead.

    Some data files were saved with mixed encoding: most characters are valid
    UTF-8 (e.g. an em dash stored as E2 80 94) but a few stray bytes come from
    Windows-1252 (e.g. 0x97, also an em dash). Strict UTF-8 decoding raises
    UnicodeDecodeError on those stray bytes. This error handler maps each
    offending byte through cp1252 so valid UTF-8 is preserved and stray bytes
    are still recovered to the intended character.
    """
    bad_bytes = error.object[error.start:error.end]
    return bad_bytes.decode("cp1252", errors="replace"), error.end


codecs.register_error("cp1252_fallback", _cp1252_fallback)  # type: ignore[arg-type]


def _decode_bytes(raw: bytes) -> str:
    """Decode file bytes as UTF-8, recovering stray Windows-1252 bytes."""
    text = raw.decode("utf-8", errors="cp1252_fallback")
    # Strip a UTF-8 BOM if present so json.loads never sees a leading \ufeff.
    return text.lstrip("\ufeff")


class JsonDataLoader:
    """Load local JSON files for the sample POC."""

    def __init__(self, base_path: Path) -> None:
        """Initialize the loader with a base directory."""
        self.base_path = base_path

    def load(self, filename: str) -> Any:
        """Load and parse a JSON file from the configured data directory."""
        file_path = self.base_path / filename
        logger.info("Loading JSON data", extra={"file_path": str(file_path)})
        try:
            raw = file_path.read_bytes()
        except FileNotFoundError as exc:
            logger.exception("JSON data file not found")
            raise DataAccessError("Required data file was not found.", {"file_path": str(file_path)}) from exc
        except OSError as exc:
            logger.exception("JSON data file could not be read")
            raise DataAccessError("Data file could not be read.", {"file_path": str(file_path)}) from exc

        try:
            return json.loads(_decode_bytes(raw))
        except json.JSONDecodeError as exc:
            logger.exception("JSON data file is invalid")
            raise DataAccessError("Data file contains invalid JSON.", {"file_path": str(file_path)}) from exc
        except (UnicodeDecodeError, ValueError) as exc:
            logger.exception("JSON data file could not be decoded")
            raise DataAccessError("Data file could not be decoded.", {"file_path": str(file_path)}) from exc


class BaseDataTools:
    """Shared base for tools that query a single JSON data file.

    Eliminates the duplicated loader setup, ``_all_records``, and
    ``_find_customer`` logic that would otherwise appear in every tool class.
    """

    def __init__(self, filename: str) -> None:
        """Initialize with the target JSON filename inside the data directory."""
        self._loader = JsonDataLoader(_DATA_DIR)
        self._filename = filename

    def _all_records(self) -> list[dict[str, Any]]:
        """Return the full JSON array from the configured data file."""
        records = self._loader.load(self._filename)
        if not isinstance(records, list):
            raise DataAccessError(
                f"{self._filename} is not in the expected list format.",
                {"file": self._filename},
            )
        return records

    def _find_customer(self, customer_id: str) -> dict[str, Any]:
        """Return the record matching *customer_id* or raise ``DataAccessError``."""
        cid = customer_id.strip().upper()
        record = next((r for r in self._all_records() if r.get("customer_id") == cid), None)
        if record is None:
            raise DataAccessError(
                "No record found for the requested customer.",
                {"customer_id": cid, "file": self._filename},
            )
        return record