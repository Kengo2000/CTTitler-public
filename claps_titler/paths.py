"""Mac / Windows の違い（保存場所・Resolve の場所・起動のしかた）をここに集める"""
import os
import sys

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
FROZEN = bool(getattr(sys, "frozen", False))      # PyInstaller でアプリにした版

HOME = os.path.expanduser("~")

if IS_WIN:
    _appdata = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    _progdata = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    _progfiles = os.environ.get("PROGRAMFILES") or r"C:\Program Files"
    SUPPORT = os.path.join(_appdata, "ClapsTitler")
    LOG_PATH = os.path.join(SUPPORT, "ClapsTitler.log")
    SAVE_ROOT = os.path.join(HOME, "Videos", "ClapsTitles")
    RESOLVE_API = os.path.join(_progdata, "Blackmagic Design", "DaVinci Resolve", "Support", "Developer", "Scripting")
    RESOLVE_DIR = os.path.join(_progfiles, "Blackmagic Design", "DaVinci Resolve")
    RESOLVE_LIB = os.path.join(RESOLVE_DIR, "fusionscript.dll")
    RESOLVE_UTILITY = os.path.join(_appdata, "Blackmagic Design", "DaVinci Resolve", "Support",
                                   "Fusion", "Scripts", "Utility")
    DEFAULT_FONT = ("Yu Gothic UI", "Bold")
else:
    SUPPORT = os.path.join(HOME, "Library", "Application Support", "ClapsTitler")
    LOG_PATH = os.path.join(HOME, "Library", "Logs", "ClapsTitler.log")
    SAVE_ROOT = os.path.join(HOME, "Movies", "ClapsTitles")
    RESOLVE_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
    RESOLVE_DIR = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion"
    RESOLVE_LIB = os.path.join(RESOLVE_DIR, "fusionscript.so")
    RESOLVE_UTILITY = os.path.join(HOME, "Library", "Application Support", "Blackmagic Design",
                                   "DaVinci Resolve", "Fusion", "Scripts", "Utility")
    DEFAULT_FONT = ("Hiragino Sans", "W6")


def keys(text):
    """画面に出すショートカット表記を OS に合わせる（Mac: ⌘N / Windows: Ctrl+N）"""
    if not IS_WIN or not text:
        return text
    return (text.replace("⇧⌘", "Ctrl+Shift+").replace("⌘↩", "Ctrl+Enter")
            .replace("⇧/⌘", "Shift/Ctrl").replace("⌘", "Ctrl+").replace("⇧", "Shift+"))


# ---------------------------------------------------------------- Resolve のスクリプトメニュー
SCRIPTS = [("起動", ""), ("新規タイトル", "--new"), ("選択タイトルを編集", "--edit-selected")]


def _lua_str(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _launch_lua(arg):
    """Resolve のメニューから CT Titler を起動する Lua の1行"""
    app_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if not FROZEN else None
    if IS_WIN:
        if FROZEN:
            cmd = f'start "" "{sys.executable}" {arg}'
        else:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.exists(pyw):
                pyw = sys.executable
            cmd = f'start "" /D "{app_dir}" "{pyw}" "{os.path.join(app_dir, "app.py")}" {arg}'
        return f'os.execute("{_lua_str(cmd)}")'
    log = LOG_PATH
    if FROZEN:
        cmd = f'nohup "{sys.executable}" {arg} > "{log}" 2>&1 &'
    else:
        cmd = f'cd "{app_dir}" && nohup "{sys.executable}" app.py {arg} > "{log}" 2>&1 &'
    return f'os.execute("{_lua_str(cmd)}")'


def install_resolve_scripts():
    """Resolve の「ワークスペース → スクリプト → Utility」に起動用スクリプトを置く（今の場所を指すように毎回確認）
    -> 書き換えたファイル数"""
    n = 0
    try:
        os.makedirs(RESOLVE_UTILITY, exist_ok=True)
        for f in os.listdir(RESOLVE_UTILITY):            # 旧名のスクリプトは片付ける
            if f.startswith("Claps Titler ") and f.endswith(".lua"):
                try:
                    os.remove(os.path.join(RESOLVE_UTILITY, f))
                except OSError:
                    pass
        for name, arg in SCRIPTS:
            body = f"-- CT Titler {name}（CT Titler が自動で作成）\n{_launch_lua(arg)}\n"
            p = os.path.join(RESOLVE_UTILITY, f"CT Titler {name}.lua")
            try:
                with open(p, encoding="utf-8") as fh:
                    if fh.read() == body:
                        continue
            except OSError:
                pass
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(body)
            n += 1
    except OSError:
        pass
    return n


def setup_log():
    """アプリ版は画面のない出力先(None)になるので、ログファイルに書き出す"""
    if sys.stdout is not None and sys.stderr is not None and not FROZEN:
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        f = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
    except OSError:
        pass
