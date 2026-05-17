"""rag-core 配置加载器。

单一配置源：D:/RAG/config.toml + D:/private/keys.ini
任何模块需要配置都从这里取，禁止自己读文件或写硬编码路径。
"""
from __future__ import annotations

import configparser
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_PATH = Path("D:/RAG/config.toml")


@dataclass
class ProviderKey:
    key: str
    base_url: str


@dataclass
class Config:
    raw: dict[str, Any]
    keys: dict[str, ProviderKey]

    # convenience
    @property
    def data_root(self) -> Path:
        return Path(self.raw["paths"]["data_root"])

    @property
    def report_dir(self) -> Path:
        return Path(self.raw["paths"]["report_dir"])

    @property
    def log_dir(self) -> Path:
        return Path(self.raw["paths"]["log_dir"])

    @property
    def state_db(self) -> Path:
        return Path(self.raw["paths"]["state_db"])

    @property
    def server_host(self) -> str:
        return self.raw["server"]["host"]

    @property
    def server_port(self) -> int:
        return int(self.raw["server"]["port"])

    @property
    def ragflow_base_url(self) -> str:
        return self.keys["ragflow"].base_url

    @property
    def ragflow_api_key(self) -> str:
        return self.keys["ragflow"].key

    @property
    def ragflow_timeout(self) -> int:
        return int(self.raw["ragflow"].get("timeout_sec", 30))

    def section(self, *keys: str) -> Any:
        node: Any = self.raw
        for k in keys:
            node = node[k]
        return node


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("rb") as f:
        return tomllib.load(f)


def _load_keys(ini_path: Path) -> dict[str, ProviderKey]:
    if not ini_path.exists():
        raise FileNotFoundError(f"keys file not found: {ini_path}")
    parser = configparser.ConfigParser()
    parser.read(ini_path, encoding="utf-8")
    result: dict[str, ProviderKey] = {}
    for section in parser.sections():
        s = parser[section]
        if "key" not in s or "base_url" not in s:
            raise ValueError(f"section [{section}] missing key/base_url")
        result[section] = ProviderKey(key=s["key"].strip(), base_url=s["base_url"].strip())
    return result


def load_config(path: Path | str = CONFIG_PATH) -> Config:
    raw = _load_toml(Path(path))
    keys_path = Path(raw["paths"]["keys_file"])
    keys = _load_keys(keys_path)
    return Config(raw=raw, keys=keys)
