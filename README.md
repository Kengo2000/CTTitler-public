# CT Titler

DaVinci Resolve 用テロッパー（別ウィンドウ型、Mac / Windows）。

- `app.py` … 起動（`python app.py`、`--new` / `--edit-selected` / `--selftest <dir>`）
- `claps_titler/` … 本体
- `.github/workflows/build.yml` … Windows / Mac でテストし、PyInstaller でアプリ化（Actions の Artifacts に zip）

検証版の使用期限は `claps_titler/trial.py`（ビルド時に自動生成）で決まる。
