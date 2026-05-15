"""File I/O + atomic write for symbol mapping cache files.

Per ``docs/phase-4-symbol-mapping-design.md`` §8 atomic write strategy:

- Tempfile + ``Path.replace()`` POSIX atomic rename on same filesystem.
- Per-signature ``asyncio.Lock`` for safe concurrent writes.
- ``.bak`` copy taken before rename for last-known-good recovery.
- Crashed-tempfile sweep on startup (D-4.A.0-8): ``.tmp`` >1h and ``.bak``
  >7d removed.

NO Redis interaction in this class. Repository is the file-layer only —
``MappingCacheService`` (step 4.A.4) wraps this and owns Redis populate /
publish so unit tests of file I/O stay free of fakeredis dependencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from .mapping_cache_schemas import RawSymbolEntry, SymbolMappingCacheFile

logger = logging.getLogger(__name__)


class MappingCacheRepository:
    """File I/O layer for cache files under ``server/data/symbol_mapping_cache/``.

    Public API:
      - ``read(signature)``         → ``SymbolMappingCacheFile | None``
      - ``read_filename(filename)`` → ``SymbolMappingCacheFile | None``
      - ``write(cache_file)``       → ``str`` (path written)
      - ``exists(signature)``       → ``bool``
      - ``list_all()``              → ``list[SymbolMappingCacheFile]``
      - ``list_filenames()``        → ``list[str]``
      - ``signature_index()``       → ``dict[str, str]``  (signature → filename)
      - ``sweep_temp_artifacts()``  → ``dict[str, int]``  ({"tmp_removed": N, "bak_removed": M})
    """

    TEMP_SUFFIX = ".tmp"
    BACKUP_SUFFIX = ".bak"
    TMP_MAX_AGE_S = 3600  # 1 hour, per D-4.A.0-8
    BAK_MAX_AGE_S = 7 * 86400  # 7 days, per D-4.A.0-8

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def cache_dir(self) -> Path:
        return self._cache_dir

    def _lock_for(self, signature: str) -> asyncio.Lock:
        """Get-or-create the per-signature lock. Idempotent."""
        lock = self._locks.get(signature)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[signature] = lock
        return lock

    def _filename_for(self, cache: SymbolMappingCacheFile) -> str:
        """Build filename per D-SM-10: ``{created_by_account}_{signature}.json``."""
        return f"{cache.created_by_account}_{cache.signature}.json"

    def _filepath_for(self, cache: SymbolMappingCacheFile) -> Path:
        return self._cache_dir / self._filename_for(cache)

    async def read(self, signature: str) -> SymbolMappingCacheFile | None:
        """Read cache by signature. Returns ``None`` if no file matches."""
        suffix = f"_{signature}.json"
        for filepath in self._cache_dir.glob("*.json"):
            if filepath.name.endswith(suffix):
                return self._read_file(filepath)
        return None

    async def read_filename(self, filename: str) -> SymbolMappingCacheFile | None:
        """Read cache by filename. Returns ``None`` if the file does not exist."""
        filepath = self._cache_dir / filename
        if not filepath.is_file():
            return None
        return self._read_file(filepath)

    def _read_file(self, filepath: Path) -> SymbolMappingCacheFile:
        """Parse + validate a cache file. Raises ``ValidationError`` on schema drift.

        Uses ``model_validate_json`` rather than ``json.load`` + ``model_validate``
        so that strict-mode datetime fields accept the on-disk ISO-8601 strings.
        """
        return SymbolMappingCacheFile.model_validate_json(
            filepath.read_text(encoding="utf-8")
        )

    async def write(self, cache: SymbolMappingCacheFile) -> str:
        """Atomic write: tempfile + rename + ``.bak`` backup of prior content.

        Algorithm (§8):
          1. Acquire per-signature lock (serialises writers for the same sig;
             different signatures run in parallel).
          2. Re-validate the model (cheap — guards against ad-hoc mutation).
          3. Write payload to ``<filepath>.tmp``.
          4. If ``<filepath>`` already exists, copy it to ``<filepath>.bak``.
          5. ``Path.replace()`` ``<filepath>.tmp`` → ``<filepath>`` (atomic).
          6. Release lock.

        On any exception during the critical section we delete the tempfile
        so a half-written ``.tmp`` does not survive past the next sweep
        window. The ``.bak`` is preserved — it is the last known good copy.
        ``updated_at`` is bumped to ``now`` before validation so the file
        on disk always reflects the most recent successful write.

        Returns the absolute path written, as a string.
        """
        cache.updated_at = datetime.now(UTC)
        filepath = self._filepath_for(cache)
        tmp_path = filepath.with_suffix(filepath.suffix + self.TEMP_SUFFIX)
        bak_path = filepath.with_suffix(filepath.suffix + self.BACKUP_SUFFIX)

        lock = self._lock_for(cache.signature)
        async with lock:
            try:
                payload = cache.model_dump_json(indent=2)
                # Re-validate via JSON round-trip; catches drift if a caller
                # mutated the model after construction. JSON path is required
                # so datetime strict-mode parsing accepts the serialised
                # ISO-8601 form rather than the raw datetime object.
                SymbolMappingCacheFile.model_validate_json(payload)

                tmp_path.write_text(payload, encoding="utf-8")

                if filepath.exists():
                    shutil.copy2(filepath, bak_path)

                tmp_path.replace(filepath)

                logger.info(
                    "mapping_cache.written",
                    extra={"signature": cache.signature, "filename": filepath.name},
                )
                return str(filepath)
            except Exception:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise

    async def exists(self, signature: str) -> bool:
        """Return ``True`` iff a cache file for ``signature`` is on disk."""
        suffix = f"_{signature}.json"
        for filepath in self._cache_dir.glob("*.json"):
            if filepath.name.endswith(suffix):
                return True
        return False

    async def delete(self, signature: str) -> bool:
        """Delete the cache file for ``signature`` if present.

        Idempotent: returns ``False`` when no matching file exists or the
        unlink races with another deleter. Used by step 4.5a's
        ``remove_account`` cleanup to drop the on-disk artefact when the
        last account referencing a signature is removed. Acquires the
        per-signature lock so a concurrent ``write`` cannot resurrect a
        half-deleted file.
        """
        suffix = f"_{signature}.json"
        lock = self._lock_for(signature)
        async with lock:
            for filepath in self._cache_dir.glob("*.json"):
                if filepath.name.endswith(suffix):
                    try:
                        filepath.unlink(missing_ok=True)
                        logger.info(
                            "mapping_cache.deleted",
                            extra={
                                "signature": signature,
                                "filename": filepath.name,
                            },
                        )
                        return True
                    except OSError as exc:
                        logger.error(
                            "mapping_cache.delete_failed",
                            extra={
                                "signature": signature,
                                "filename": filepath.name,
                                "error": str(exc),
                            },
                        )
                        return False
            return False

    async def list_all(self) -> list[SymbolMappingCacheFile]:
        """Load and validate all cache files. Used at server startup.

        Corrupt / schema-drifted files are logged at ERROR and skipped so a
        single bad file cannot prevent boot. The healthy files load.
        """
        result: list[SymbolMappingCacheFile] = []
        for filepath in sorted(self._cache_dir.glob("*.json")):
            try:
                result.append(self._read_file(filepath))
            except Exception as exc:
                logger.error(
                    "mapping_cache.load_failed",
                    extra={"filepath": str(filepath), "error": str(exc)},
                )
        return result

    async def list_filenames(self) -> list[str]:
        """Return sorted cache filenames without parsing content."""
        return sorted(p.name for p in self._cache_dir.glob("*.json"))

    async def signature_index(self) -> dict[str, str]:
        """Build a ``signature → filename`` index from filenames only.

        Used by the index endpoint (step 4.A.4) where the caller wants the
        list of available signatures without paying for full file parses.
        Filenames that do not match the ``{account}_{sig}.json`` pattern
        are silently skipped — corrupt-naming entries should not appear in
        the index. ``list_all()`` is the right path to surface load errors.
        """
        index: dict[str, str] = {}
        for filepath in self._cache_dir.glob("*.json"):
            stem = filepath.stem
            if "_" in stem:
                _, sig = stem.rsplit("_", 1)
                index[sig] = filepath.name
        return index

    def sweep_temp_artifacts(self) -> dict[str, int]:
        """Server-startup sweep of leftover ``.tmp`` and ``.bak`` files.

        Per D-4.A.0-8: ``.tmp`` older than ``TMP_MAX_AGE_S`` (1h) and
        ``.bak`` older than ``BAK_MAX_AGE_S`` (7d) are deleted. Synchronous
        because called during lifespan init before the event loop is
        receiving traffic — there is no parallel write to coordinate with.
        Returns counts for logging / tests.
        """
        now = time.time()
        tmp_removed = 0
        bak_removed = 0

        for filepath in self._cache_dir.iterdir():
            if not filepath.is_file():
                continue
            try:
                age_s = now - filepath.stat().st_mtime
            except OSError:
                continue
            if filepath.name.endswith(self.TEMP_SUFFIX) and age_s > self.TMP_MAX_AGE_S:
                try:
                    filepath.unlink()
                    tmp_removed += 1
                    logger.warning(
                        "mapping_cache.tmp_swept",
                        extra={"filepath": str(filepath), "age_s": int(age_s)},
                    )
                except OSError as exc:
                    logger.error(
                        "mapping_cache.tmp_sweep_failed",
                        extra={"filepath": str(filepath), "error": str(exc)},
                    )
            elif filepath.name.endswith(self.BACKUP_SUFFIX) and age_s > self.BAK_MAX_AGE_S:
                try:
                    filepath.unlink()
                    bak_removed += 1
                    logger.info(
                        "mapping_cache.bak_swept",
                        extra={"filepath": str(filepath), "age_s": int(age_s)},
                    )
                except OSError as exc:
                    logger.error(
                        "mapping_cache.bak_sweep_failed",
                        extra={"filepath": str(filepath), "error": str(exc)},
                    )

        return {"tmp_removed": tmp_removed, "bak_removed": bak_removed}


def compute_signature(raw_symbols: list[RawSymbolEntry]) -> str:
    """Sig-1 per D-SM-03 + design §3.

    SHA-256 hex digest of the JSON array of *sorted* symbol names. Sort
    order is the only normalisation; any other field of ``RawSymbolEntry``
    (digits, contract_size, etc.) is intentionally excluded so that a
    broker spec drift on an existing symbol invalidates downstream
    comparisons via the spec-level diff in step 4.A.4, not via this
    signature.

    Free function (not a method) — useful for callers that have a raw
    snapshot in hand and do not need a repository instance.
    """
    names = sorted(s.name for s in raw_symbols)
    payload = json.dumps(names, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
