"""タイトルのデータ構造（JSONでそのまま保存できる辞書）"""
import copy
import json
import os
import uuid

MAX_EDGES = 4

from .paths import DEFAULT_FONT

DEFAULT_TITLE = {
    "version": 1,
    "kind": "text",          # text=文字のテロップ / image=画像のテロップ（ロゴ・帯・飾り）
    "canvas": [1920, 1080],
    "text": "タイトル",
    "image": {"file": "", "name": "", "w": 100, "h": 100, "opacity": 1.0},   # 画像のテロップのとき
    "follow": "",            # 画像をいっしょに流すロール／クロールのテロップ（uid）
    "font": {
        "family": DEFAULT_FONT[0],
        "style": DEFAULT_FONT[1],
        "size": 96,
        "tracking": 0.0,     # 字間(px)
        "palt": False,       # 自動で詰める（プロポーショナル: かな・約物を字面に合わせて詰める）※画面からは外した
        "edge_gap": True,    # 文字ごとの指定で縁が太い文字は、隣との間を縁の差だけ自動で空ける
        "leading": 1.2,      # 行間(倍)
        "align": "center",   # left / center / right
    },
    "pos": [960, 900],       # 文字ブロックのアンカー位置(px)
    "anchor": [0.5, 0.5],    # アンカー [横, 縦] 0=左/上 0.5=中 1=右/下（旧形式 "mc" なども読める。回転・拡大の中心にもなる）
    "face": {
        "enabled": True,
        "color": "#ffffff",
        "gradient": False,
        "color2": "#ffd200",
        # 多色グラデーション: [位置0〜1, 色, 不透明度]
        "stops": [[0.0, "#ffffff", 1.0], [1.0, "#ffd200", 1.0]],
        "angle": 90,          # 90=上→下 / 0=左→右
        "span": "line",       # line=行ごと / block=文字全体
        "opacity": 1.0,
        "emboss": {"on": False, "depth": 1.0, "blur": 4.0, "angle": 225},   # 面のエンボス（強さ・幅・光の向き）
        "img_on": False,      # 画像で塗る（色・グラデーションの代わり）
        "img": {"file": "", "name": "", "w": 0, "h": 0, "fit": "cover", "span": "block", "scale": 100.0},
    },
    # エッジ1が文字に一番近い。外側へ順に重なる
    "edges": [
        {"enabled": True, "width": 8, "color": "#000000", "opacity": 1.0, "soft": 0},
        {"enabled": False, "width": 6, "color": "#ffffff", "opacity": 1.0, "soft": 0},
        {"enabled": False, "width": 6, "color": "#000000", "opacity": 1.0, "soft": 0},
        {"enabled": False, "width": 6, "color": "#ffffff", "opacity": 1.0, "soft": 0},
    ],
    "shadow": {
        "enabled": True,
        "color": "#000000",
        "opacity": 0.6,
        "distance": 8,
        "angle": 45,         # 影が落ちる方向 0=右 90=下
        "blur": 6,
    },
    "extrude": {             # 疑似押し出し
        "enabled": False,
        "depth": 20,
        "angle": 45,
        "color": "#303030",
        "opacity": 1.0,
        "grad": False,       # 手前の色 → 奥の色のグラデーション
        "color2": "#000000",
    },
    "inner": {               # カットアウトシャドー（文字の内側の影）
        "enabled": False,
        "color": "#000000",
        "opacity": 0.6,
        "distance": 6,
        "angle": 45,         # 光の来る向きと逆（影が寄る向き）0=右 90=下
        "blur": 4,
    },
    "box": {                 # 座布団
        "enabled": False,
        "color": "#000000",
        "opacity": 0.5,
        "padx": 40,
        "pady": 16,
        "radius": 8,
        "mode": "fit",       # fit=文字に追従 / free=位置・サイズを独立
        "rect": [960, 900, 1200, 140],   # free時: 中心X, 中心Y, 幅, 高さ
    },
    "anim": {                # 簡易イン/アウト
        "in": "none", "out": "none", "dur": 0.5, "ease_in": "cubic", "ease_out": "cubic",
        "zoom": 2.0, "blur": 30.0, "motion_blur": False,
        "pivot_mode": "auto",      # 拡大の中心: auto=テロップの中心 / screen=画面の中心 / custom=指定
        "pivot": [960, 540],
    },
    "xf": {"rot": 0.0, "sx": 100.0, "sy": 100.0},   # テロップ自体の回転(°・反時計回り)と拡大(%)
    "roll": {                # ロール（縦に流す）／クロール（横に流す）
        "enabled": False,
        "dir": "up",         # up=ロール（下から上へ） / left=クロール（右から左へ） / right=クロール（左から右へ）
        "start": "below",    # below=画面の外から入る / here=今の位置から
        "end": "above",      # above=画面の外へ抜ける / center=画面中央で止める（ロールは最後の行） / here=今の位置で止める
        "speed": "fit",      # fit=尺に合わせる / ppf=1フレームあたりのピクセル数
        "ppf": 4,
        "snap": True,        # 1フレームの移動量を整数ピクセルに（ちらつき防止）
        "repeat": "single",  # single=1回だけ / loop=くり返す（外から入って外へ抜けるをくり返す）
    },
}

for _e in DEFAULT_TITLE["edges"]:          # 縁のグラデーション（既定は単色）
    _e.update({"gradient": False, "stops": [[0.0, _e["color"], 1.0], [1.0, "#ffffff", 1.0]], "angle": 90,
               "span": "line"})

# スタイルとして保存・適用する項目（文字内容と位置は含めない）
STYLE_KEYS = ["font", "face", "edges", "shadow", "extrude", "inner", "box", "anim"]


def new_uid():
    return uuid.uuid4().hex[:5]


def new_image(width, height, file, w, h, name=""):
    """画像のテロップ（ロゴ・帯・飾り）。位置は画像の中心。大きさ・回転は xf"""
    t = new_title(width, height)
    t["kind"] = "image"
    t["text"], t["runs"] = "", []
    t["image"] = {"file": file, "name": name or os.path.basename(file), "w": int(w), "h": int(h), "opacity": 1.0}
    t["anchor"] = [0.5, 0.5]
    t["pos"] = [width / 2, height / 2]
    t["shadow"]["enabled"] = False
    t["follow"] = ""          # いっしょに流すロール／クロールのテロップ（uid）
    k = min(1.0, width * 0.9 / max(1, w), height * 0.9 / max(1, h))   # 大きすぎる画像は画面に収める
    t["xf"] = {"rot": 0.0, "sx": round(k * 100, 2), "sy": round(k * 100, 2)}
    return t


def has_content(o):
    """置くものがあるか（文字が入っているか、画像のテロップ）"""
    return o.get("kind") == "image" or bool((o.get("text") or "").strip())


def new_title(width=1920, height=1080):
    t = copy.deepcopy(DEFAULT_TITLE)
    t["uid"] = new_uid()
    t["canvas"] = [int(width), int(height)]
    t["pos"] = [width / 2, round(height * 0.83)]
    t["font"]["size"] = round(96 * height / 1080)
    return t


def normalize(t):
    """古いファイルや欠けた項目をデフォルトで補う"""
    base = copy.deepcopy(DEFAULT_TITLE)
    old_face = t.get("face", {}) if isinstance(t, dict) else {}

    def merge(dst, src):
        for k, v in src.items():
            if k in dst and isinstance(dst[k], dict) and isinstance(v, dict):
                merge(dst[k], v)
            else:
                dst[k] = v
    merge(base, t)
    if "stops" not in old_face:            # 旧形式の2色グラデを多色に移す
        base["face"]["stops"] = [[0.0, base["face"]["color"], 1.0], [1.0, base["face"]["color2"], 1.0]]
    edges = list(base.get("edges", []))[:MAX_EDGES]
    for e_ in edges:                          # 縁のグラデーション（なければ単色）
        e_.setdefault("gradient", False)
        e_.setdefault("stops", [[0.0, e_.get("color", "#000000"), 1.0], [1.0, "#ffffff", 1.0]])
        e_.setdefault("angle", 90)
        e_.setdefault("span", "line")
    while len(edges) < MAX_EDGES:
        edges.append(copy.deepcopy(DEFAULT_TITLE["edges"][len(edges)]))
    for i, e in enumerate(edges):        # 古いファイルのエッジに欠けた項目を補う
        for k, v in DEFAULT_TITLE["edges"][min(i, 3)].items():
            e.setdefault(k, v)
    base["edges"] = edges
    base.setdefault("uid", new_uid())
    if not base.get("uid"):
        base["uid"] = new_uid()
    return base


def extract_style(t):
    return copy.deepcopy({k: t[k] for k in STYLE_KEYS if k in t})


def compress_runs(styles):
    """文字ごとの指定のリスト → runs [{"s": 開始, "e": 終了(含まない), ...指定}]（同じ指定が続く所はまとめる）"""
    out = []
    key = lambda a: json.dumps(a, sort_keys=True)
    i, n = 0, len(styles)
    while i < n:
        a = styles[i] or {}
        j = i
        while j + 1 < n and key(styles[j + 1] or {}) == key(a):
            j += 1
        if a:
            r = {"s": i, "e": j + 1}
            r.update(a)
            out.append(r)
        i = j + 1
    return out


def scale_deco(t, k):
    """文字サイズに合わせて装飾（字間・縁・影・押し出し・座布団の余白と角丸）を k 倍する"""
    if abs(k - 1.0) < 1e-9 or k <= 0:
        return t
    f = t.get("font") or {}
    if "tracking" in f:
        f["tracking"] = float(f.get("tracking", 0)) * k
    for e in t.get("edges") or []:
        e["width"] = float(e.get("width", 0)) * k
        e["soft"] = float(e.get("soft", 0)) * k
    sh = t.get("shadow") or {}
    for key in ("distance", "blur"):
        if key in sh:
            sh[key] = float(sh[key]) * k
    ex = t.get("extrude") or {}
    if "depth" in ex:
        ex["depth"] = max(1.0, float(ex["depth"]) * k)
    emb = (t.get("face") or {}).get("emboss") or {}
    if "blur" in emb:
        emb["blur"] = float(emb["blur"]) * k
    ins = t.get("inner") or {}
    for key in ("distance", "blur"):
        if key in ins:
            ins[key] = float(ins[key]) * k
    bx = t.get("box") or {}
    for key in ("padx", "pady", "radius"):
        if key in bx:
            bx[key] = float(bx[key]) * k
    scale_runs(t, k, deco=True)
    return t


def scale_runs(t, k, deco=False):
    """文字ごとの指定のうち、px で持っている値を k 倍する
    カーニング・ベースラインは文字の配置なので常に、縁・影の数値は deco=True のときだけ"""
    if abs(k - 1.0) < 1e-9 or k <= 0:
        return t
    for r in t.get("runs") or []:
        for key in ("kern", "bs") + (("sh_dist", "sh_blur") if deco else ()):
            if key in r and r[key] is not None:
                r[key] = round(float(r[key]) * k, 2)
        if deco:
            for key in ("ew", "es"):
                if isinstance(r.get(key), dict):
                    r[key] = {i: round(float(v) * k, 3) for i, v in r[key].items()}
    return t


def apply_style(t, style, keep_size=False):
    """keep_size=True なら文字サイズは今のまま、装飾はそのサイズに合わせて縮尺して当てる"""
    size = t["font"]["size"]
    size0 = float(size)
    for k in STYLE_KEYS:
        if k in style:
            t[k] = copy.deepcopy(style[k])
    if keep_size:
        ssize = float((style.get("font") or {}).get("size") or size)
        t["font"]["size"] = size
        scale_deco(t, float(size) / max(1.0, ssize))
        scale_runs(t, max(1.0, ssize) / float(size), deco=True)   # 文字ごとの指定は元のまま（上で掛けた分を戻す）
    else:
        scale_runs(t, float(t["font"]["size"]) / max(1.0, size0))   # 文字サイズが変わった分、カーニング等も合わせる
    return normalize(t)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---- 複数テロップ（ドキュメント）
GROUP_ANIM = {"enabled": False, "in": "none", "out": "none", "dur": 0.5, "ease_in": "cubic", "ease_out": "cubic",
              "zoom": 2.0, "blur": 30.0, "motion_blur": False, "pivot_mode": "auto", "pivot": [960, 540]}


def new_doc(width=1920, height=1080, style=None):
    t = new_title(width, height)
    if style:
        t = apply_style(t, style)
    return {"version": 2, "canvas": [int(width), int(height)], "mode": "separate", "objects": [t],
            "ganim": copy.deepcopy(GROUP_ANIM)}


def normalize_doc(d):
    if "objects" not in d:                      # 旧形式（1テロップ）
        d = {"version": 2, "canvas": d.get("canvas", [1920, 1080]), "mode": "single", "objects": [d]}
    objs = [normalize(o) for o in d.get("objects") or []] or [new_title(*d["canvas"])]
    seen = set()
    for o in objs:
        o["canvas"] = list(d["canvas"])
        if o["uid"] in seen:
            o["uid"] = new_uid()
        seen.add(o["uid"])
    ga = copy.deepcopy(GROUP_ANIM)
    ga.update(d.get("ganim") or {})
    out = {"version": 2, "canvas": list(d["canvas"]), "mode": d.get("mode", "single"), "objects": objs,
           "ganim": ga}
    if d.get("source"):
        out["source"] = d["source"]
    return out


def doc_from_sidecar(data):
    if "doc" in data:
        return normalize_doc(data["doc"])
    return normalize_doc(data["title"])


# グラデーションのプリセット
GRADIENT_PRESETS = {
    "ゴールド": [[0.0, "#d68e34", 1], [0.2, "#d79133", 1], [0.45, "#f9df4d", 1], [0.7, "#dc9f37", 1], [1.0, "#d6a73f", 1]],
    "シルバー": [[0.0, "#f4f6f8", 1], [0.45, "#9aa3ad", 1], [0.55, "#6c757f", 1], [1.0, "#e6eaee", 1]],
    "ブロンズ": [[0.0, "#f0c08a", 1], [0.5, "#a0602a", 1], [1.0, "#e2a46a", 1]],
    "ファイヤー": [[0.0, "#fff3a0", 1], [0.4, "#ffb000", 1], [1.0, "#e52a00", 1]],
    "アイス": [[0.0, "#ffffff", 1], [0.5, "#9be3ff", 1], [1.0, "#2a8bd6", 1]],
    "白→黄": [[0.0, "#ffffff", 1], [1.0, "#ffd200", 1]],
}
