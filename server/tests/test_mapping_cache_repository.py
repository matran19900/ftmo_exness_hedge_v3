"""Tests for ``MappingCacheRepository`` + cache file Pydantic schemas (Phase 4.A.2).

Coverage matrix per step plan §2.7:
  - §2.7.1 Schema validation (RawSymbolEntry / MappingEntry / SymbolMappingCacheFile)
  - §2.7.2 Repository write paths (happy / overwrite / concurrency / failures)
  - §2.7.3 Repository read paths (by signature / by filename / corrupt)
  - §2.7.4 list_all
  - §2.7.5 signature_index
  - §2.7.6 sweep_temp_artifacts
  - §2.7.7 compute_signature
  - §2.7.8 Integration roundtrip
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.services.mapping_cache_repository import (
    MappingCacheRepository,
    compute_signature,
)
from app.services.mapping_cache_schemas import (
    MappingEntry,
    RawSymbolEntry,
    SymbolMappingCacheFile,
)
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _raw_symbol(name: str = "EURUSDz", **overrides: object) -> RawSymbolEntry:
    base = {
        "name": name,
        "contract_size": 100000.0,
        "digits": 5,
        "pip_size": 0.0001,
        "volume_min": 0.01,
        "volume_step": 0.01,
        "volume_max": 200.0,
        "currency_profit": "USD",
    }
    base.update(overrides)
    return RawSymbolEntry(**base)  # type: ignore[arg-type]


def _mapping(ftmo: str = "EURUSD", exness: str = "EURUSDz") -> MappingEntry:
    return MappingEntry(
        ftmo=ftmo,
        exness=exness,
        match_type="suffix_strip",
        contract_size=100000.0,
        pip_size=0.0001,
        pip_value=10.0,
        quote_ccy="USD",
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )


def _cache_file(
    *,
    signature: str = "sig-test-1",
    created_by: str = "exn_001",
    used_by: list[str] | None = None,
    raw: list[RawSymbolEntry] | None = None,
    mappings: list[MappingEntry] | None = None,
) -> SymbolMappingCacheFile:
    now = datetime.now(UTC)
    return SymbolMappingCacheFile(
        schema_version=1,
        signature=signature,
        created_at=now,
        updated_at=now,
        created_by_account=created_by,
        used_by_accounts=list(used_by) if used_by is not None else [created_by],
        raw_symbols_snapshot=raw if raw is not None else [_raw_symbol()],
        mappings=mappings if mappings is not None else [_mapping()],
    )


@pytest.fixture
def repo(tmp_path: Path) -> MappingCacheRepository:
    """Per-test repository instance pinned to a fresh tmp_path."""
    return MappingCacheRepository(tmp_path)


# ---------------------------------------------------------------------------
# §2.7.1 — Schema validation tests
# ---------------------------------------------------------------------------


class TestRawSymbolEntrySchema:
    def test_happy_path(self) -> None:
        entry = _raw_symbol()
        assert entry.name == "EURUSDz"
        assert entry.digits == 5
        assert entry.currency_profit == "USD"

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            RawSymbolEntry(
                name="EURUSDz",
                contract_size=100000.0,
                digits=5,
                pip_size=0.0001,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=200.0,
                currency_profit="USD",
                surprise=True,  # type: ignore[call-arg]
            )

    def test_strict_string_rejected_for_float(self) -> None:
        # strict=True: a string in a float field raises ValidationError
        # (ints-for-floats are accepted in pydantic v2 strict mode).
        with pytest.raises(ValidationError):
            RawSymbolEntry(
                name="EURUSDz",
                contract_size="100000.0",  # type: ignore[arg-type]
                digits=5,
                pip_size=0.0001,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=200.0,
                currency_profit="USD",
            )

    def test_strict_float_rejected_for_int(self) -> None:
        with pytest.raises(ValidationError):
            RawSymbolEntry(
                name="EURUSDz",
                contract_size=100000.0,
                digits=5.0,  # type: ignore[arg-type]
                pip_size=0.0001,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=200.0,
                currency_profit="USD",
            )

    def test_currency_profit_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            _raw_symbol(currency_profit="USDX")
        with pytest.raises(ValidationError):
            _raw_symbol(currency_profit="US")

    def test_missing_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RawSymbolEntry(  # type: ignore[call-arg]
                name="EURUSDz",
                contract_size=100000.0,
                digits=5,
                pip_size=0.0001,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=200.0,
                # currency_profit missing
            )


class TestMappingEntrySchema:
    def test_happy_path_has_10_fields(self) -> None:
        entry = _mapping()
        assert set(entry.model_dump().keys()) == {
            "ftmo",
            "exness",
            "match_type",
            "contract_size",
            "pip_size",
            "pip_value",
            "quote_ccy",
            "exness_volume_step",
            "exness_volume_min",
            "exness_volume_max",
        }

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MappingEntry(
                ftmo="EURUSD",
                exness="EURUSDz",
                match_type="suffix_strip",
                contract_size=100000.0,
                pip_size=0.0001,
                pip_value=10.0,
                quote_ccy="USD",
                exness_volume_step=0.01,
                exness_volume_min=0.01,
                exness_volume_max=200.0,
                exotic_field="oops",  # type: ignore[call-arg]
            )

    def test_missing_volume_step_rejected(self) -> None:
        # D-4.A.0-2 additions are required (no default)
        with pytest.raises(ValidationError):
            MappingEntry(  # type: ignore[call-arg]
                ftmo="EURUSD",
                exness="EURUSDz",
                match_type="suffix_strip",
                contract_size=100000.0,
                pip_size=0.0001,
                pip_value=10.0,
                quote_ccy="USD",
                # exness_volume_step missing
                exness_volume_min=0.01,
                exness_volume_max=200.0,
            )

    def test_quote_ccy_length_enforced(self) -> None:
        with pytest.raises(ValidationError):
            MappingEntry(
                ftmo="EURUSD",
                exness="EURUSDz",
                match_type="suffix_strip",
                contract_size=100000.0,
                pip_size=0.0001,
                pip_value=10.0,
                quote_ccy="USDX",
                exness_volume_step=0.01,
                exness_volume_min=0.01,
                exness_volume_max=200.0,
            )


class TestSymbolMappingCacheFileSchema:
    def test_happy_path(self) -> None:
        cf = _cache_file()
        assert cf.schema_version == 1
        assert cf.signature == "sig-test-1"
        assert cf.created_by_account == "exn_001"

    def test_extra_top_level_field_forbidden(self) -> None:
        cf_dict = _cache_file().model_dump(mode="json")
        cf_dict["unknown_top_level"] = "boom"
        with pytest.raises(ValidationError):
            SymbolMappingCacheFile.model_validate(cf_dict)

    def test_missing_required_field_rejected(self) -> None:
        cf_dict = _cache_file().model_dump(mode="json")
        del cf_dict["signature"]
        with pytest.raises(ValidationError):
            SymbolMappingCacheFile.model_validate(cf_dict)

    def test_nested_extra_field_forbidden(self) -> None:
        cf_dict = _cache_file().model_dump(mode="json")
        cf_dict["raw_symbols_snapshot"][0]["extra_nested"] = 1
        with pytest.raises(ValidationError):
            SymbolMappingCacheFile.model_validate(cf_dict)

    def test_schema_version_must_be_positive(self) -> None:
        cf_dict = _cache_file().model_dump(mode="json")
        cf_dict["schema_version"] = 0
        with pytest.raises(ValidationError):
            SymbolMappingCacheFile.model_validate(cf_dict)


# ---------------------------------------------------------------------------
# §2.7.2 — Repository write tests
# ---------------------------------------------------------------------------


class TestRepositoryWrite:
    @pytest.mark.asyncio
    async def test_write_creates_file_with_d_sm_10_naming(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        cf = _cache_file(signature="abc123", created_by="exn_001")
        path = await repo.write(cf)
        assert path == str(tmp_path / "exn_001_abc123.json")
        assert (tmp_path / "exn_001_abc123.json").is_file()

    @pytest.mark.asyncio
    async def test_write_payload_validates_with_pydantic(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        cf = _cache_file()
        await repo.write(cf)
        on_disk_text = (tmp_path / "exn_001_sig-test-1.json").read_text()
        # Round-trip via JSON path so strict-mode datetimes parse from string.
        SymbolMappingCacheFile.model_validate_json(on_disk_text)

    @pytest.mark.asyncio
    async def test_write_overwrite_creates_bak(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        cf = _cache_file(signature="dup")
        await repo.write(cf)
        # second write of same sig must produce .bak with previous payload.
        cf2 = _cache_file(
            signature="dup",
            mappings=[_mapping(ftmo="GBPUSD", exness="GBPUSDz")],
        )
        await repo.write(cf2)
        bak = tmp_path / "exn_001_dup.json.bak"
        assert bak.is_file()
        bak_content = json.loads(bak.read_text())
        # bak should hold the FIRST write's content (single EURUSD mapping).
        assert bak_content["mappings"][0]["ftmo"] == "EURUSD"

    @pytest.mark.asyncio
    async def test_write_first_time_no_bak(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        await repo.write(_cache_file(signature="fresh"))
        assert not (tmp_path / "exn_001_fresh.json.bak").exists()

    @pytest.mark.asyncio
    async def test_write_bumps_updated_at(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        old_ts = datetime(2020, 1, 1, tzinfo=UTC)
        cf = _cache_file()
        cf.updated_at = old_ts
        await repo.write(cf)
        assert cf.updated_at > old_ts

    @pytest.mark.asyncio
    async def test_write_concurrent_same_signature_serialised(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        """Two writers on the same signature must not interleave inside the
        critical section. We assert this by holding the per-sig lock manually
        and observing that an in-flight write blocks until released."""
        cf = _cache_file(signature="ser")
        held = repo._lock_for("ser")
        await held.acquire()
        try:
            task = asyncio.create_task(repo.write(cf))
            # Give the task a chance to reach the lock.
            await asyncio.sleep(0.05)
            assert not task.done(), "writer should be blocked on locked sig"
        finally:
            held.release()
        await task
        assert (tmp_path / "exn_001_ser.json").is_file()

    @pytest.mark.asyncio
    async def test_write_concurrent_different_signatures_parallel(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        """Distinct signatures use distinct locks → no serialisation."""
        cf1 = _cache_file(signature="par1")
        cf2 = _cache_file(signature="par2")
        await asyncio.gather(repo.write(cf1), repo.write(cf2))
        assert (tmp_path / "exn_001_par1.json").is_file()
        assert (tmp_path / "exn_001_par2.json").is_file()

    @pytest.mark.asyncio
    async def test_write_failure_cleans_up_tmp(
        self, repo: MappingCacheRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If shutil.copy2 raises (e.g. permission), the tempfile must not
        survive — otherwise a future sweep (or read) would see a phantom."""
        cf = _cache_file(signature="willfail")
        # Pre-create the target file so the .bak copy path runs.
        await repo.write(cf)

        # Now monkeypatch shutil.copy2 to raise for the second write.
        import app.services.mapping_cache_repository as mod

        def _boom(_src: object, _dst: object) -> None:
            raise OSError("simulated copy failure")

        monkeypatch.setattr(mod.shutil, "copy2", _boom)

        with pytest.raises(OSError, match="simulated copy failure"):
            await repo.write(cf)

        # tempfile must NOT persist
        assert not (tmp_path / "exn_001_willfail.json.tmp").exists()


# ---------------------------------------------------------------------------
# §2.7.3 — Repository read tests
# ---------------------------------------------------------------------------


class TestRepositoryRead:
    @pytest.mark.asyncio
    async def test_read_by_signature_happy_path(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="sigR1"))
        loaded = await repo.read("sigR1")
        assert loaded is not None
        assert loaded.signature == "sigR1"

    @pytest.mark.asyncio
    async def test_read_by_signature_missing_returns_none(
        self, repo: MappingCacheRepository
    ) -> None:
        assert await repo.read("does-not-exist") is None

    @pytest.mark.asyncio
    async def test_read_by_filename_happy_path(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="sigR2", created_by="exn_xxx"))
        loaded = await repo.read_filename("exn_xxx_sigR2.json")
        assert loaded is not None
        assert loaded.created_by_account == "exn_xxx"

    @pytest.mark.asyncio
    async def test_read_by_filename_missing_returns_none(
        self, repo: MappingCacheRepository
    ) -> None:
        assert await repo.read_filename("nope.json") is None

    @pytest.mark.asyncio
    async def test_exists_true_and_false(
        self, repo: MappingCacheRepository
    ) -> None:
        assert await repo.exists("missing") is False
        await repo.write(_cache_file(signature="sigE"))
        assert await repo.exists("sigE") is True


# ---------------------------------------------------------------------------
# §2.7.4 — list_all tests
# ---------------------------------------------------------------------------


class TestListAll:
    @pytest.mark.asyncio
    async def test_empty_folder(self, repo: MappingCacheRepository) -> None:
        assert await repo.list_all() == []

    @pytest.mark.asyncio
    async def test_multiple_files_sorted(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="z", created_by="exn_a"))
        await repo.write(_cache_file(signature="a", created_by="exn_b"))
        loaded = await repo.list_all()
        assert len(loaded) == 2
        # Sorted by filename → exn_a_z.json before exn_b_a.json
        assert loaded[0].created_by_account == "exn_a"
        assert loaded[1].created_by_account == "exn_b"

    @pytest.mark.asyncio
    async def test_corrupt_file_skipped_with_log(
        self,
        repo: MappingCacheRepository,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        await repo.write(_cache_file(signature="good"))
        (tmp_path / "exn_x_corrupt.json").write_text("{not valid json", encoding="utf-8")

        with caplog.at_level("ERROR"):
            loaded = await repo.list_all()

        assert len(loaded) == 1
        assert loaded[0].signature == "good"
        assert any(
            "mapping_cache.load_failed" in record.message for record in caplog.records
        )


# ---------------------------------------------------------------------------
# §2.7.5 — signature_index tests
# ---------------------------------------------------------------------------


class TestSignatureIndex:
    @pytest.mark.asyncio
    async def test_index_maps_sig_to_filename(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="s1", created_by="exn_a"))
        await repo.write(_cache_file(signature="s2", created_by="exn_b"))
        index = await repo.signature_index()
        assert index == {
            "s1": "exn_a_s1.json",
            "s2": "exn_b_s2.json",
        }

    @pytest.mark.asyncio
    async def test_malformed_filename_skipped(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        # No underscore in stem → silently skipped (corrupt naming).
        (tmp_path / "weirdname.json").write_text("{}", encoding="utf-8")
        await repo.write(_cache_file(signature="ok"))
        index = await repo.signature_index()
        assert "ok" in index
        assert "weirdname" not in index

    @pytest.mark.asyncio
    async def test_index_does_not_parse_files(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        # Write a syntactically broken file with a valid filename pattern;
        # signature_index() reads filenames only and must NOT raise.
        (tmp_path / "exn_q_brokensig.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        index = await repo.signature_index()
        assert index["brokensig"] == "exn_q_brokensig.json"


# ---------------------------------------------------------------------------
# §2.7.6 — sweep_temp_artifacts tests
# ---------------------------------------------------------------------------


def _set_mtime(p: Path, age_seconds: float) -> None:
    """Set ``p``'s mtime to ``now - age_seconds``."""
    import time as _time

    target = _time.time() - age_seconds
    os.utime(p, (target, target))


class TestSweepTempArtifacts:
    def test_old_tmp_removed(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        old = tmp_path / "exn_x_oldsig.json.tmp"
        old.write_text("crashed", encoding="utf-8")
        _set_mtime(old, 7200)  # 2h > 1h threshold
        result = repo.sweep_temp_artifacts()
        assert result["tmp_removed"] == 1
        assert not old.exists()

    def test_recent_tmp_preserved(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        recent = tmp_path / "exn_x_freshsig.json.tmp"
        recent.write_text("in-flight", encoding="utf-8")
        _set_mtime(recent, 60)  # 1 min — well under 1h
        result = repo.sweep_temp_artifacts()
        assert result["tmp_removed"] == 0
        assert recent.exists()

    def test_old_bak_removed(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        old = tmp_path / "exn_x_oldbak.json.bak"
        old.write_text("ancient", encoding="utf-8")
        _set_mtime(old, 8 * 86400)  # 8 days > 7 days
        result = repo.sweep_temp_artifacts()
        assert result["bak_removed"] == 1
        assert not old.exists()

    def test_recent_bak_preserved(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        recent = tmp_path / "exn_x_recentbak.json.bak"
        recent.write_text("yesterday", encoding="utf-8")
        _set_mtime(recent, 86400)  # 1 day
        result = repo.sweep_temp_artifacts()
        assert result["bak_removed"] == 0
        assert recent.exists()

    def test_non_tmp_or_bak_files_ignored(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        # A normal .json file, even ancient, must NEVER be touched.
        normal = tmp_path / "exn_x_perm.json"
        normal.write_text("{}", encoding="utf-8")
        _set_mtime(normal, 365 * 86400)  # 1 year
        result = repo.sweep_temp_artifacts()
        assert result == {"tmp_removed": 0, "bak_removed": 0}
        assert normal.exists()

    def test_empty_folder_noop(
        self, repo: MappingCacheRepository
    ) -> None:
        assert repo.sweep_temp_artifacts() == {"tmp_removed": 0, "bak_removed": 0}

    def test_mixed_fresh_and_stale(
        self, repo: MappingCacheRepository, tmp_path: Path
    ) -> None:
        stale_tmp = tmp_path / "a_stale.json.tmp"
        stale_tmp.write_text("", encoding="utf-8")
        _set_mtime(stale_tmp, 4000)
        fresh_tmp = tmp_path / "b_fresh.json.tmp"
        fresh_tmp.write_text("", encoding="utf-8")
        _set_mtime(fresh_tmp, 60)
        stale_bak = tmp_path / "c_stale.json.bak"
        stale_bak.write_text("", encoding="utf-8")
        _set_mtime(stale_bak, 30 * 86400)
        fresh_bak = tmp_path / "d_fresh.json.bak"
        fresh_bak.write_text("", encoding="utf-8")
        _set_mtime(fresh_bak, 86400)

        result = repo.sweep_temp_artifacts()
        assert result == {"tmp_removed": 1, "bak_removed": 1}
        assert not stale_tmp.exists()
        assert fresh_tmp.exists()
        assert not stale_bak.exists()
        assert fresh_bak.exists()


# ---------------------------------------------------------------------------
# §2.7.7 — compute_signature tests
# ---------------------------------------------------------------------------


class TestComputeSignature:
    def test_deterministic_same_input_same_hash(self) -> None:
        symbols = [_raw_symbol("EURUSD"), _raw_symbol("GBPUSD")]
        assert compute_signature(symbols) == compute_signature(list(symbols))

    def test_sort_order_insensitive(self) -> None:
        a = [_raw_symbol("EURUSD"), _raw_symbol("GBPUSD")]
        b = [_raw_symbol("GBPUSD"), _raw_symbol("EURUSD")]
        assert compute_signature(a) == compute_signature(b)

    def test_different_symbols_different_hashes(self) -> None:
        single = [_raw_symbol("EURUSD")]
        two = [_raw_symbol("EURUSD"), _raw_symbol("GBPUSD")]
        assert compute_signature(single) != compute_signature(two)

    def test_empty_list_returns_valid_hash(self) -> None:
        # sha256 of `[]` is well-defined; assert it is hex-shaped.
        sig = compute_signature([])
        assert isinstance(sig, str)
        assert len(sig) == 64
        int(sig, 16)  # raises if not hex

    def test_only_name_matters_other_fields_ignored(self) -> None:
        a = [_raw_symbol("EURUSD", contract_size=100000.0, digits=5)]
        b = [_raw_symbol("EURUSD", contract_size=10000.0, digits=3)]
        # Same name → same signature even though specs diverged.
        assert compute_signature(a) == compute_signature(b)


# ---------------------------------------------------------------------------
# §2.7.8 — Integration tests
# ---------------------------------------------------------------------------


class TestRepositoryIntegration:
    @pytest.mark.asyncio
    async def test_write_read_roundtrip(
        self, repo: MappingCacheRepository
    ) -> None:
        cf = _cache_file(
            signature="round1",
            raw=[_raw_symbol("EURUSD"), _raw_symbol("GBPUSD")],
            mappings=[
                _mapping("EURUSD", "EURUSDz"),
                _mapping("GBPUSD", "GBPUSDz"),
            ],
        )
        await repo.write(cf)
        loaded = await repo.read("round1")
        assert loaded is not None
        assert loaded.signature == "round1"
        assert {m.ftmo for m in loaded.mappings} == {"EURUSD", "GBPUSD"}
        assert {s.name for s in loaded.raw_symbols_snapshot} == {"EURUSD", "GBPUSD"}

    @pytest.mark.asyncio
    async def test_three_writes_then_signature_index_returns_three(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="i1", created_by="exn_1"))
        await repo.write(_cache_file(signature="i2", created_by="exn_2"))
        await repo.write(_cache_file(signature="i3", created_by="exn_3"))
        index = await repo.signature_index()
        assert set(index.keys()) == {"i1", "i2", "i3"}

    @pytest.mark.asyncio
    async def test_list_filenames_sorted(
        self, repo: MappingCacheRepository
    ) -> None:
        await repo.write(_cache_file(signature="s1", created_by="zz"))
        await repo.write(_cache_file(signature="s2", created_by="aa"))
        names = await repo.list_filenames()
        assert names == ["aa_s2.json", "zz_s1.json"]

    @pytest.mark.asyncio
    async def test_repository_creates_dir_if_missing(
        self, tmp_path: Path
    ) -> None:
        nested = tmp_path / "deeply" / "nested" / "cache"
        repo = MappingCacheRepository(nested)
        assert nested.is_dir()
        assert repo.cache_dir == nested
