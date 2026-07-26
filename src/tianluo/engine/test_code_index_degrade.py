"""Co-located tests for the code-index degrade (line/byte chunking) mode.

The degrade mode is the LAST-RESORT granularity: a file falls back to mechanical
chunking ONLY when all three conditions hold simultaneously — text (non-binary),
zero structural units, and over the size threshold. These tests pin the
three-condition gate, the chunk granularity, and the ``[degraded:chunk]``
annotation; they live beside the engine source as a controlled co-located
exception (see base *Engine Co-located Test Modules*).
"""

from __future__ import annotations

from pathlib import Path

from tianluo.config import CodeIndexConfig
from tianluo.engine import code_index
from tianluo.engine.code_index import DEGRADED_MARKER, is_degrade_eligible


def _small_cfg() -> CodeIndexConfig:
    # Tiny thresholds so tests can trip degrade with a few lines.
    return CodeIndexConfig(
        degrade_trigger_lines=5,
        degrade_trigger_bytes=1024 * 1024,  # don't trip on bytes here
        chunk_lines=3,
        chunk_bytes=1024 * 1024,
    )


class TestDegradeGate:
    def test_all_three_conditions_required(self):
        cfg = _small_cfg()
        big = "\n".join(f"line {i}" for i in range(20))  # > 5 lines
        small = "\n".join(f"line {i}" for i in range(3))  # <= 5 lines

        # (2)+(3): structure-less AND over threshold => eligible.
        assert is_degrade_eligible(big, has_structure=False, cfg=cfg) is True
        # has_structure True voids it even when large.
        assert is_degrade_eligible(big, has_structure=True, cfg=cfg) is False
        # under threshold voids it even when structure-less.
        assert is_degrade_eligible(small, has_structure=False, cfg=cfg) is False

    def test_byte_threshold_trips_independently(self):
        cfg = CodeIndexConfig(
            degrade_trigger_lines=10_000,  # don't trip on lines
            degrade_trigger_bytes=10,
            chunk_lines=3,
            chunk_bytes=1024,
        )
        text = "abcdefghijklmnop"  # > 10 bytes, 1 line
        assert is_degrade_eligible(text, has_structure=False, cfg=cfg) is True


class TestChunking:
    def test_chunks_respect_line_limit_and_mark_degraded(self):
        cfg = _small_cfg()
        text = "\n".join(f"line {i}" for i in range(10))  # 10 lines
        chunks = code_index._chunk_degraded(text, cfg)
        assert len(chunks) >= 3
        for ch in chunks:
            assert ch.degraded is True
            assert ch.kind == "chunk"
            assert (ch.line_end - ch.line_start + 1) <= cfg.chunk_lines

    def test_small_structureless_file_stops_at_file_level(self, tmp_path: Path):
        cfg = _small_cfg()
        f = tmp_path / "tiny.txt"
        f.write_text("a\nb\n", encoding="utf-8")  # 2 lines, under threshold
        fe = code_index._index_file(f, "tiny.txt", cfg)
        assert fe.kind == "text"
        assert fe.symbols == []  # no chunks: stays a single file-level line

    def test_large_structureless_file_degrades_to_chunks(self, tmp_path: Path):
        cfg = _small_cfg()
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"row {i}" for i in range(30)), encoding="utf-8")
        fe = code_index._index_file(f, "big.txt", cfg)
        assert fe.symbols, "expected degraded chunks"
        assert all(s.degraded for s in fe.symbols)

    def test_degraded_marker_in_render(self, tmp_path: Path):
        from tianluo.engine import code_index_render

        cfg = _small_cfg()
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"row {i}" for i in range(30)), encoding="utf-8")
        fe = code_index._index_file(f, "big.txt", cfg)
        index = code_index.CodeIndex(project_root=tmp_path)
        index.files["big.txt"] = fe
        out = code_index_render.render_path(index, "big.txt")
        assert DEGRADED_MARKER in out
