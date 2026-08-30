"""vault 单测：配置持久化/路径拼装/三态检查（开发计划 M6）。

配置函数通过可选 path 参数注入 tmp 目录，绝不触碰真实 HOME。
"""

import json
from pathlib import Path

import pytest

from subtitle_cli.vault import (
    VaultConfig,
    check_vault,
    collection_root,
    load_config,
    save_config,
)


# ---- 配置持久化 ----


def test_config_roundtrip(tmp_path):
    target = tmp_path / "config.json"
    cfg = VaultConfig(vault="D:/Obsidian/MyVault", subdir="学习/B站字幕")
    save_config(cfg, target)
    assert load_config(target) == cfg


def test_load_missing_file_returns_defaults(tmp_path):
    assert load_config(tmp_path / "absent.json") == VaultConfig()


def test_load_corrupt_json_returns_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_config(target) == VaultConfig()


def test_load_wrong_schema_returns_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text(json.dumps({"vault": 123}), encoding="utf-8")
    assert load_config(target) == VaultConfig()


def test_save_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nest" / "config.json"
    save_config(VaultConfig(vault="D:/v"), target)
    assert load_config(target).vault == "D:/v"


# ---- 路径拼装 ----


def test_collection_root_joins_subdir():
    root = collection_root(
        VaultConfig(vault="D:/Obsidian/MyVault", subdir="学习/B站字幕")
    )
    assert root == Path("D:/Obsidian/MyVault") / "学习" / "B站字幕"


def test_collection_root_empty_subdir_is_vault_root():
    assert collection_root(VaultConfig(vault="D:/v", subdir="  ")) == Path("D:/v")


def test_collection_root_empty_vault_raises():
    with pytest.raises(ValueError, match="vault"):
        collection_root(VaultConfig(vault="  "))


# ---- 三态检查 ----


def test_check_vault_ok_root(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / ".obsidian").mkdir()
    status = check_vault(str(vault))
    assert status.ok and status.writable and status.is_vault_root


def test_check_vault_warning_subdirectory(tmp_path):
    sub = tmp_path / "vault" / "笔记"
    sub.mkdir(parents=True)
    status = check_vault(str(sub))
    assert status.ok and status.writable
    assert not status.is_vault_root
    assert ".obsidian" in status.message


def test_check_vault_missing_path_offers_create(tmp_path):
    status = check_vault(str(tmp_path / "nope"))
    assert not status.ok
    assert status.can_create


def test_check_vault_create_makes_it_usable(tmp_path):
    target = tmp_path / "created" / "vault"
    status = check_vault(str(target), create=True)
    assert target.is_dir()
    assert status.ok and status.writable
    assert not status.is_vault_root  # 新建的目录自然没有 .obsidian


def test_check_vault_rejects_plain_file(tmp_path):
    target = tmp_path / "afile.txt"
    target.write_text("x", encoding="utf-8")
    status = check_vault(str(target))
    assert not status.ok
    assert not status.can_create
    assert "不是文件夹" in status.message


def test_check_vault_empty_path():
    status = check_vault("   ")
    assert not status.ok
    assert "为空" in status.message
