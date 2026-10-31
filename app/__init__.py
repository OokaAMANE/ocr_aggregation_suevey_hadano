"""アンケート OCR・集計ツール。

起動時に、プロジェクト直下の .env ファイル（KEY=値 の形式）があれば環境変数として読み込む。
Windows で環境変数を設定しなくても、.env に ANTHROPIC_API_KEY を書けば使えるようにするため。
すでに設定されている環境変数のほうが優先される。
"""

import os
from pathlib import Path


def _load_env_file(path: Path):
    if not path.is_file():
        return
    # メモ帳で保存すると先頭に BOM が付くことがあるため utf-8-sig で読む
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(Path(__file__).resolve().parent.parent / ".env")
