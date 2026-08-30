"""迁移单测（M8，PRD F4 / 验收 7-10）：扫描、转换、跳过、覆盖、演练与离线性。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from subtitle_cli.migration import (
    format_migration_summary,
    migrate,
    scan_collections,
)
from subtitle_cli.vault import VaultConfig, load_config, save_config

runner = CliRunner()
FIXED_DATE = date(2026, 1, 2)


def make_legacy(tmp_path: Path) -> Path:
    root = tmp_path / "old"
    cdir = root / "旧合集"
    cdir.mkdir(parents=True)
    (cdir / "EP01 第一集.md").write_text(
        "# 第1集 第一集\n\n大家好。\n\n再见。\n", encoding="utf-8"
    )
    (cdir / "EP02 第二集.md").write_text("# 第2集 第二集\n\n内容A。\n", encoding="utf-8")
    (cdir / "乱起名.md").write_text("# 不合规范\n", encoding="utf-8")
    (cdir / "坏编码.md").write_bytes(b"\xff\xfe\x00bad")
    (root / "散文件.md").write_text("# 野文件\n", encoding="utf-8")
    return root


def make_vault_cfg(tmp_path: Path) -> tuple[VaultConfig, Path]:
    cfg = VaultConfig(vault=str(tmp_path / "vault"), subdir="B站字幕")
    return cfg, tmp_path / "vault"


# ---- 扫描 ----


def test_scan_collections_classifies(tmp_path: Path):
    root = make_legacy(tmp_path)
    scan = scan_collections(root)
    assert [c.name for c in scan.collections] == ["旧合集"]
    c = scan.collections[0]
    assert len(c.pending) == 2
    assert len(c.unparsable) == 2  # 乱起名 + 坏编码
    assert c.already == []
    assert scan.loose_files == [str(root / "散文件.md")]


def test_scan_missing_root_raises(tmp_path: Path):
    import pytest

    with pytest.raises(ValueError, match="不存在"):
        scan_collections(tmp_path / "absent")


# ---- 迁移主流程 ----


def test_migrate_end_to_end(tmp_path: Path):
    root = make_legacy(tmp_path)
    cfg, vault = make_vault_cfg(tmp_path)
    logs: list[str] = []
    outcome = migrate(root, cfg, log=logs.append, fetched_at=FIXED_DATE)

    target = vault / "B站字幕" / "旧合集"
    note = (target / "EP01 第一集.md").read_text(encoding="utf-8")
    assert note.startswith("---\n")
    assert "source: \"\"" in note and "author: \"\"" in note  # 缺失字段留空
    assert "tags:\n  - B站字幕\n  - 旧合集" in note
    assert "# 第1集 第一集\n\n大家好。\n\n再见。\n" in note  # 正文一字不动
    assert (target / "EP02 第二集.md").exists()

    index = (target / "旧合集.md").read_text(encoding="utf-8")
    assert "type: index" in index and "episodes: 2" in index
    assert "- [[EP01 第一集|第1集 第一集]]" in index

    assert outcome.results[0].index_episodes == 2
    summary = format_migration_summary(outcome)
    assert "成功 2（迁移 2、跳过 0）" in summary
    assert "失败 2" in summary  # 乱起名 + 坏编码
    assert "缺失字段：source、author" in summary
    assert "散落文件 1 个" in summary


def test_migrate_rerun_skips_all(tmp_path: Path):
    root = make_legacy(tmp_path)
    cfg, _ = make_vault_cfg(tmp_path)
    migrate(root, cfg, log=lambda *_: None, fetched_at=FIXED_DATE)
    outcome = migrate(root, cfg, log=lambda *_: None, fetched_at=FIXED_DATE)
    statuses = [f.status for r in outcome.results for f in r.files if f.status != "failed"]
    assert set(statuses) == {"skipped"}
    summary = format_migration_summary(outcome)
    assert "迁移 0" in summary


def test_migrate_does_not_overwrite_extraction_artifacts(tmp_path: Path):
    """目标名被提取产物占用（无 fetched_by 标记）时永远跳过，--overwrite 也不例外。"""
    root = make_legacy(tmp_path)
    cfg, vault = make_vault_cfg(tmp_path)
    target = vault / "B站字幕" / "旧合集" / "EP01 第一集.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ntitle: x\n---\n\n# 提取产物正文\n", encoding="utf-8")
    for overwrite in (False, True):
        outcome = migrate(root, cfg, overwrite=overwrite, log=lambda *_: None, fetched_at=FIXED_DATE)
        files = {Path(f.source).name: f for r in outcome.results for f in r.files}
        assert files["EP01 第一集.md"].status == "skipped"
        assert "提取产物" in (files["EP01 第一集.md"].reason or "")
        assert target.read_text(encoding="utf-8").endswith("# 提取产物正文\n")
        assert not files["EP01 第一集.md"].target or files["EP01 第一集.md"].status == "skipped"
    # EP02 不受影响，正常迁移
    assert (vault / "B站字幕" / "旧合集" / "EP02 第二集.md").exists()


def test_migrate_overwrite_rewrites_marker_files(tmp_path: Path):
    root = make_legacy(tmp_path)
    cfg, vault = make_vault_cfg(tmp_path)
    migrate(root, cfg, log=lambda *_: None, fetched_at=FIXED_DATE)
    outcome = migrate(root, cfg, overwrite=True, log=lambda *_: None, fetched_at=FIXED_DATE)
    files = {Path(f.source).name: f for r in outcome.results for f in r.files}
    assert files["EP01 第一集.md"].status == "migrated"  # 覆盖重写
    assert files["EP02 第二集.md"].status == "migrated"
    note = (vault / "B站字幕" / "旧合集" / "EP01 第一集.md").read_text(encoding="utf-8")
    assert "大家好。" in note  # 重写后内容仍完整


def test_migrate_dry_run_writes_nothing(tmp_path: Path):
    root = make_legacy(tmp_path)
    cfg, vault = make_vault_cfg(tmp_path)
    outcome = migrate(root, cfg, dry_run=True, log=lambda *_: None, fetched_at=FIXED_DATE)
    assert not vault.exists()  # 零写盘（验收 10）
    summary = format_migration_summary(outcome)
    assert "演练，未写盘" in summary
    assert "成功 2（迁移 2、跳过 0）" in summary  # 计划仍可见


def test_migrate_selected_collections(tmp_path: Path):
    root = tmp_path / "old"
    for name in ("合集A", "合集B"):
        d = root / name
        d.mkdir(parents=True)
        (d / "EP01 一.md").write_text("# 第1集 一\n\n正文\n", encoding="utf-8")
    cfg, vault = make_vault_cfg(tmp_path)
    outcome = migrate(root, cfg, ["合集A"], log=lambda *_: None, fetched_at=FIXED_DATE)
    assert [r.name for r in outcome.results] == ["合集A"]
    assert (vault / "B站字幕" / "合集A" / "EP01 一.md").exists()
    assert not (vault / "B站字幕" / "合集B").exists()


# ---- CLI 入口 ----


def _patch(monkeypatch, tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.json"
    monkeypatch.setattr("subtitle_cli.vault.config_path", lambda: cfg_path)
    return cfg_path


def make_clean_legacy(tmp_path: Path) -> Path:
    """只含合法旧笔记的目录（无失败项，可得到退出码 0）。"""
    root = tmp_path / "clean-old"
    cdir = root / "旧合集"
    cdir.mkdir(parents=True)
    (cdir / "EP01 第一集.md").write_text("# 第1集 第一集\n\n大家好。\n", encoding="utf-8")
    (cdir / "EP02 第二集.md").write_text("# 第2集 第二集\n\n内容A。\n", encoding="utf-8")
    return root


def test_migrate_cli_end_to_end(tmp_path: Path, monkeypatch):
    root = make_clean_legacy(tmp_path)
    vault = tmp_path / "vault"
    cfg_path = _patch(monkeypatch, tmp_path)
    from subtitle_cli.migrate import app

    result = runner.invoke(app, [str(root), "--vault", str(vault)])
    assert result.exit_code == 0, result.output
    assert (vault / "B站字幕" / "旧合集" / "EP01 第一集.md").exists()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert cfg["vault"] == str(vault)
    assert load_config(cfg_path).vault == str(vault)


def test_migrate_cli_dry_run_exit_zero(tmp_path: Path, monkeypatch):
    root = make_clean_legacy(tmp_path)
    _patch(monkeypatch, tmp_path)
    from subtitle_cli.migrate import app

    result = runner.invoke(app, [str(root), "--vault", str(tmp_path / "v"), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "v").exists()


def test_migrate_cli_with_failures_exits_1(tmp_path: Path, monkeypatch):
    """无法解析的文件计入失败 → 退出码 1（PRD §10 / migrate 模块约定）。"""
    root = make_legacy(tmp_path)
    _patch(monkeypatch, tmp_path)
    from subtitle_cli.migrate import app

    result = runner.invoke(app, [str(root), "--vault", str(tmp_path / "v")])
    assert result.exit_code == 1
    assert "失败 2" in result.output


def test_migrate_cli_missing_dir_exits_2(tmp_path: Path, monkeypatch):
    _patch(monkeypatch, tmp_path)
    from subtitle_cli.migrate import app

    result = runner.invoke(app, [str(tmp_path / "absent"), "--vault", str(tmp_path / "v")])
    assert result.exit_code == 2
    assert "输入无效" in result.output


# ---- 离线性（开发计划 §0） ----


def test_migration_modules_never_import_bilibili():
    code = (
        "import sys, subtitle_cli.migration, subtitle_cli.migrate; "
        "bad = [m for m in sys.modules if 'bilibili' in m]; "
        "assert not bad, bad"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
