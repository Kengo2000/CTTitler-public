"""CT Titler — DaVinci Resolve 用テロッパー（別ウィンドウ型）"""
import copy
import datetime
import getpass
import json
import math
import os
import re
import sys
import traceback

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QTextCharFormat, QTextCursor, QCursor, QFontMetricsF, QAction, QColor, QFont, QFontDatabase, QIcon,
                           QImage, QKeySequence, QPainter, QPen, QPixmap,
                           QShortcut)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QMenu, QStyle, QStyledItemDelegate, QSlider, QAbstractItemView, QApplication, QButtonGroup,
                               QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDoubleSpinBox, QFileDialog, QFontComboBox,
                               QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QListView,
                               QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QSplitter,
                               QTabWidget, QToolButton, QVBoxLayout, QWidget, QFrame,
                               QAbstractSpinBox, QLineEdit)

from . import model
from . import render as R
from . import textplus
from .gradient_editor import GradientEditor
from . import textplus_out as TO
from . import anim as A
from . import roll as RL
from .resolve_bridge import (COMPOUND_PREFIX, Bridge, ResolveError,
                             parse_png, png_name)

APP_NAME = "CT Titler"
APP_VERSION = "1.4"
APP_TAGLINE = "DaVinci Resolve 用テロッパー"
from .paths import IS_WIN, SUPPORT, SAVE_ROOT, keys, install_resolve_scripts, setup_log
SETTINGS_PATH = os.path.join(SUPPORT, "settings.json")
STYLES_PATH = os.path.join(SUPPORT, "styles.json")
BG_PATH = os.path.join(SUPPORT, "background.png")
SESSION_PATH = os.path.join(SUPPORT, "session.json")
INDEX_PATH = os.path.join(SUPPORT, "titles_index.json")   # グループID → 保存フォルダ
SERVER = "ClapsTitler-" + getpass.getuser()

MODES = [("テロップごとに別クリップ", "separate"), ("別クリップ → 複合クリップ", "compound"),
         ("まとめて1枚", "single")]
MODES_TP = [("テロップごとに別タイトル", "separate"), ("別タイトル → 複合クリップ", "compound"),
            ("（1つのタイトルにまとめる・Inspectorは1つ目のみ）", "single")]


# ------------------------------------------------------------ 部品
class ColorButton(QPushButton):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(54, 24)
        self.setCursor(Qt.PointingHandCursor)
        self._c = "#ffffff"
        self.clicked.connect(self.pick)

    def color(self):
        return self._c

    def setColor(self, c):
        self._c = QColor(c).name()
        self.setStyleSheet(f"QPushButton{{background:{self._c};border:1px solid #777;border-radius:3px;}}")

    def pick(self):
        old = self._c
        dlg = QColorDialog(QColor(old), self)
        dlg.setWindowTitle("色を選択")
        dlg.setOption(QColorDialog.DontUseNativeDialog, True)

        def live(c):
            self.setColor(c.name())
            self.changed.emit()
        dlg.currentColorChanged.connect(live)
        if dlg.exec() != QDialog.Accepted:
            self.setColor(old)
        else:
            self.setColor(dlg.selectedColor().name())
        self.changed.emit()


def spin(lo, hi, step=1, dec=0, suffix=""):
    w = QDoubleSpinBox() if dec else QSpinBox()
    if dec:
        w.setDecimals(dec)
    w.setRange(lo, hi)
    w.setSingleStep(step)
    if suffix:
        w.setSuffix(suffix)
    w.setMinimumWidth(80)
    return w


def obj_label(o):
    if o.get("kind") == "image":
        return "🖼 " + str((o.get("image") or {}).get("name") or "画像")[:20]
    first = (o["text"].strip().splitlines() or ["（空）"])[0]
    return first[:20]


# ------------------------------------------------------------ プレビュー
class FontPreviewDelegate(QStyledItemDelegate):
    """フォント一覧の各行に、そのフォントで見本の文字を描く"""
    def __init__(self, sample_fn, parent=None):
        super().__init__(parent)
        self.sample_fn = sample_fn

    _jp = {}

    def _has_jp(self, fam):
        if fam not in self._jp:
            self._jp[fam] = QFontDatabase.WritingSystem.Japanese in QFontDatabase.writingSystems(fam)
        return self._jp[fam]

    def sizeHint(self, option, index):
        return QSize(420, 36)

    def paint(self, p, option, index):
        fam = index.data(Qt.DisplayRole) or ""
        p.save()
        r = option.rect
        if option.state & QStyle.State_Selected:
            p.fillRect(r, option.palette.highlight())
            fg = option.palette.highlightedText().color()
        else:
            fg = option.palette.text().color()
        # 左: フォント名（普通の書体で小さく）
        nf = QFont(option.font)
        nf.setPointSizeF(max(9.0, nf.pointSizeF() * 0.9))
        p.setFont(nf)
        sub = QColor(fg)
        sub.setAlpha(170)
        p.setPen(sub)
        name_w = 170
        p.drawText(QRectF(r.left() + 6, r.top(), name_w - 8, r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft | Qt.TextSingleLine,
                   QFontMetricsF(nf).elidedText(fam, Qt.ElideRight, name_w - 10))
        # 右: そのフォントで見本。和文の無いフォントは欧文の見本にして「欧文」と添える
        sample = self.sample_fn()
        x0 = r.left() + name_w
        if not self._has_jp(fam) and any(ord(ch) > 0x2E7F for ch in sample):
            p.setPen(sub)
            p.drawText(QRectF(x0, r.top(), 34, r.height()), Qt.AlignVCenter | Qt.AlignLeft, "欧文")
            x0 += 36
            sample = "Aa Bb Cc 123"
        ff = QFont(fam)
        ff.setPixelSize(22)
        p.setFont(ff)
        p.setPen(fg)
        p.drawText(QRectF(x0, r.top(), r.right() - x0 - 6, r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft | Qt.TextSingleLine, sample)
        p.restore()


class FontCombo(QFontComboBox):
    """一覧を閉じたことを知らせるフォント選択（カーソルを合わせたフォントを仮表示するため）"""
    popupClosed = Signal()

    def hidePopup(self):
        super().hidePopup()
        self.popupClosed.emit()


_ROT_CURSOR = None


def _rotate_cursor():
    """回転用のカーソル（丸い矢印）"""
    global _ROT_CURSOR
    if _ROT_CURSOR is None:
        from PySide6.QtGui import QPainterPath, QPolygonF
        pm = QPixmap(28, 28)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.arcMoveTo(5, 5, 18, 18, 200)
        path.arcTo(5, 5, 18, 18, 200, -220)
        tip1 = path.pointAtPercent(1.0)
        tip0 = path.pointAtPercent(0.0)
        for col, w in ((QColor(0, 0, 0), 4.5), (QColor(255, 255, 255), 2.0)):
            p.setPen(QPen(col, w, Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        p.setPen(QPen(QColor(0, 0, 0), 1.2))
        p.setBrush(QColor(255, 255, 255))
        for tip, d in ((tip1, 1), (tip0, -1)):          # 両端に矢じり
            p.drawPolygon(QPolygonF([tip + QPointF(-5 * d, -3), tip + QPointF(4 * d, 1), tip + QPointF(-3 * d, 5)]))
        p.end()
        _ROT_CURSOR = QCursor(pm, 14, 14)
    return _ROT_CURSOR


class StyledTextEdit(QPlainTextEdit):
    """文字入力欄: コピー＆ペーストで文字ごとの指定も一緒に運ぶ"""
    copy_hook = None      # (開始, 終了) -> bytes
    paste_hook = None     # (QMimeData) -> True なら貼り付け済み
    mime_type = "application/x-ct-titler-styled-text"

    def createMimeDataFromSelection(self):
        md = super().createMimeDataFromSelection()
        c = self.textCursor()
        if self.copy_hook and c.hasSelection():
            try:
                data = self.copy_hook(c.selectionStart(), c.selectionEnd())
                if data:
                    md.setData(self.mime_type, data)
            except Exception:
                traceback.print_exc()
        return md

    def insertFromMimeData(self, source):
        if self.paste_hook and source.hasFormat(self.mime_type):
            try:
                if self.paste_hook(source):
                    return
            except Exception:
                traceback.print_exc()
        super().insertFromMimeData(source)


class Preview(QWidget):
    picked = Signal(int, bool)            # テロップ番号, 追加選択か
    pivotPicked = Signal(float, float)    # 拡大の中心をクリックで指定
    xfChanged = Signal(float, float, float)   # ハンドル操作: 回転(°), 横%, 縦%
    moved = Signal(str, float, float)     # 対象("text"/"box"/"all"), 移動量(キャンバス座標)
    charSelected = Signal(int, int)       # 文字の選択(開始, 終了)。-1,-1 で文字選択をやめる
    charNudge = Signal(float, float)      # 文字選択中の ⌥矢印: カーニング, ベースライン の増減(px)
    textInput = Signal(str)               # 文字編集中に打った文字（確定したもの）
    textCommand = Signal(str)             # 文字編集中のキー操作: backspace/delete/copy/cut/paste
    newTextAt = Signal(float, float)      # 何もない所をダブルクリック: そこに新しいテロップを作って打ち込む

    def __init__(self):
        super().__init__()
        self.setMinimumSize(480, 270)
        self.setFocusPolicy(Qt.StrongFocus)
        self.bg = None
        self.bg_mode = "resolve"
        self.image = None
        self.infos = []
        self.free_box = []            # テロップごとに座布団が「固定」か
        self.sel = set()
        self.cur = 0
        self.canvas = (1920, 1080)
        self.safe = True
        self.target = "text"
        self._drag = None
        self._off = QPointF()
        self._static = None
        self._moving = None
        self.split_fn = None          # (target) -> (動かない画像, 動かす画像)
        self.xf_ctx = None            # 現在のテロップ: (pos, xf)
        self._hdrag = None
        self.keep_aspect = lambda: True
        # 文字選択（ダブルクリックで入る）
        self.char_mode = False
        self.cur_image = False        # 今のテロップが画像（文字の編集はしない）
        self.char_boxes = []          # 文字ごとの枠（回転・拡大の前、キャンバス座標）
        self.line_starts = [0]
        self.char_sel = None          # (開始, 終了)
        self._csel_anchor = None
        self.preedit = ""             # 日本語入力の変換中の文字
        self.empty_caret = None       # 文字が空のときのカーソル（キャンバス座標の上端・下端）
        self.setAttribute(Qt.WA_InputMethodEnabled, True)

    def set_image(self, img, infos, canvas, free_box, sel, cur):
        self.image, self.infos, self.canvas = img, infos, canvas
        self.free_box, self.sel, self.cur = free_box, set(sel), cur
        self._off = QPointF()
        if self.target == "box" and not (cur < len(free_box) and free_box[cur]):
            self.target = "text"
        self.update()

    def _fit(self):
        W, H = self.canvas
        r = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        s = min(r.width() / W, r.height() / H)
        w, h = W * s, H * s
        return QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h), s

    # ---- 文字選択
    def _char_xf(self):
        if self.xf_ctx is None:
            from PySide6.QtGui import QTransform
            return QTransform()
        return self._xf_transform(*self.xf_ctx)

    def _lines(self):
        """[(開始, 文字数)] 行ごと"""
        n = len(self.char_boxes)
        out = []
        for i, ls in enumerate(self.line_starts):
            le = (self.line_starts[i + 1] - 1) if i + 1 < len(self.line_starts) else n
            out.append((ls, max(0, le - ls)))
        return out

    def _caret_at(self, c):
        """キャンバス座標 c に一番近い文字の間（カーソル位置）"""
        if not self.char_boxes:
            return 0
        inv, ok = self._char_xf().inverted()
        q = inv.map(c) if ok else c
        best, bd = None, None
        for ls, ln in self._lines():
            rs = [self.char_boxes[i] for i in range(ls, min(ls + ln + 1, len(self.char_boxes)))]
            rs = [r for r in rs if r.height() > 0]
            if not rs:
                continue
            top = min(r.top() for r in rs)
            bot = max(r.bottom() for r in rs)
            d = 0 if top <= q.y() <= bot else min(abs(q.y() - top), abs(q.y() - bot))
            if bd is None or d < bd:
                best, bd = (ls, ln), d
        if best is None:
            return 0
        ls, ln = best
        for i in range(ls, ls + ln):
            if q.x() < self.char_boxes[i].center().x():
                return i
        return ls + ln

    def _char_under(self, c):
        """キャンバス座標 c の文字の番号（ダブルクリック用）"""
        k = self._caret_at(c)
        inv, ok = self._char_xf().inverted()
        q = inv.map(c) if ok else c
        n = len(self.char_boxes)
        if k < n and self.char_boxes[k].width() > 0 and q.x() >= self.char_boxes[k].left():
            return k
        return max(0, k - 1)

    def _in_text(self, c):
        return (self.cur < len(self.infos) and
                self.infos[self.cur]["text"].adjusted(-8, -8, 8, 8).contains(c))

    def set_char_sel(self, sel, emit=True):
        self.char_sel = sel
        if emit:
            if sel is None:
                self.charSelected.emit(-1, -1)
            else:
                self.charSelected.emit(*sel)
        self.update()

    def exit_char_mode(self, emit=True):
        if self.char_mode or self.char_sel:
            self.char_mode = False
            self._csel_anchor = None
            self.setCursor(Qt.ArrowCursor)
            self.set_char_sel(None, emit)

    def _caret_line(self, k):
        """カーソル位置 k の縦線（キャンバス座標の上端・下端、回転・拡大の前）"""
        n = len(self.char_boxes)
        if not n:
            return self.empty_caret
        if k < n and self.char_boxes[k].height() > 0:
            r = self.char_boxes[k]
            return r.topLeft(), r.bottomLeft()
        j = min(k, n) - 1
        while j >= 0 and self.char_boxes[j].height() <= 0:
            j -= 1
        if j < 0:
            return None
        r = self.char_boxes[j]
        return r.topRight(), r.bottomRight()

    def inputMethodEvent(self, e):
        if not self.char_mode:
            return super().inputMethodEvent(e)
        if e.commitString():
            self.preedit = ""
            self.textInput.emit(e.commitString())
        self.preedit = e.preeditString()
        self.update()
        e.accept()

    def inputMethodQuery(self, q):
        if q == Qt.ImEnabled:
            return bool(self.char_mode)
        if q == Qt.ImCursorRectangle and self.char_mode and self.char_sel:
            ln = self._caret_line(self.char_sel[1])
            if ln:
                xf = self._char_xf()
                a, b = self._to_widget(xf.map(ln[0])), self._to_widget(xf.map(ln[1]))
                from PySide6.QtCore import QRect
                return QRect(int(a.x()), int(a.y()), 2, max(4, int(b.y() - a.y())))
        return super().inputMethodQuery(q)

    def mouseDoubleClickEvent(self, e):
        c = self._to_canvas(e.position())
        if (e.button() == Qt.LeftButton and len(self.sel) <= 1 and not self.char_boxes
                and self.empty_caret is not None):
            self.char_mode = True                    # 空のテロップ: そのまま打ち込める状態に
            self.set_char_sel((0, 0))
            self.setCursor(Qt.IBeamCursor)
            return
        if e.button() == Qt.LeftButton and self._hit(c) is None and (self.char_boxes or self.cur_image):
            self.newTextAt.emit(c.x(), c.y())
            return
        if e.button() == Qt.LeftButton and len(self.sel) <= 1 and self._in_text(c) and self.char_boxes:
            self.char_mode = True
            k = self._char_under(c)
            n = len(self.char_boxes)
            # 改行の上なら、その手前の文字
            self.set_char_sel((k, min(n, k + 1)))
            self.setCursor(Qt.IBeamCursor)
            return
        super().mouseDoubleClickEvent(e)

    def _to_canvas(self, pos):
        rect, s = self._fit()
        return QPointF((pos.x() - rect.x()) / s, (pos.y() - rect.y()) / s)

    def _hit(self, c):
        """一番上のテロップから当たり判定。(番号, "text"/"box") か None"""
        for i in range(len(self.infos) - 1, -1, -1):
            if self.infos[i]["text"].adjusted(-8, -8, 8, 8).contains(c):
                return i, "text"
        for i in range(len(self.infos) - 1, -1, -1):
            b = self.infos[i].get("box")
            if b is not None and self.free_box[i] and b.contains(c):
                return i, "box"
        return None

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        p.fillRect(self.rect(), QColor("#1e1e1e"))
        rect, s = self._fit()
        if self.bg_mode == "resolve" and self.bg is not None:
            p.drawImage(rect, self.bg)
        elif self.bg_mode == "black":
            p.fillRect(rect, Qt.black)
        elif self.bg_mode == "gray":
            p.fillRect(rect, QColor("#808080"))
        else:
            p.save()
            p.setClipRect(rect)
            n = 12
            for y in range(int(rect.height() // n) + 1):
                for x in range(int(rect.width() // n) + 1):
                    p.fillRect(QRectF(rect.x() + x * n, rect.y() + y * n, n, n),
                               QColor("#9a9a9a") if (x + y) % 2 else QColor("#6a6a6a"))
            p.restore()
        if self.image is not None:
            if self._drag is not None and self._moving is not None:
                if self._static is not None:
                    p.drawImage(rect, self._static)
                p.drawImage(rect.translated(self._off.x() * s, self._off.y() * s), self._moving)
            else:
                p.drawImage(rect, self.image)
        if self.safe:
            p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.DashLine))
            for k in (0.05, 0.1):
                p.drawRect(rect.adjusted(rect.width() * k, rect.height() * k,
                                         -rect.width() * k, -rect.height() * k))
            p.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.DotLine))
            p.drawLine(QPointF(rect.center().x(), rect.top()), QPointF(rect.center().x(), rect.bottom()))
        # バウンディングボックス（現在のテロップ）
        hs = self._handles()
        if hs and self._drag is None:
            poly = [hs[k] for k in ("tl", "tr", "br", "bl")]
            p.setPen(QPen(QColor(255, 255, 255, 200), 1))
            for a_, b_ in zip(poly, poly[1:] + poly[:1]):
                p.drawLine(self._to_widget(a_), self._to_widget(b_))
            p.drawLine(self._to_widget(hs["tc"]), self._to_widget(hs["rot"]))
            for k, c in hs.items():
                wpt = self._to_widget(c)
                if k == "rot":
                    p.setBrush(QColor(80, 200, 120))
                    p.drawEllipse(wpt, 5, 5)
                elif k == "anchor":
                    p.setBrush(QColor(255, 150, 40))
                    p.drawRect(QRectF(wpt.x() - 4, wpt.y() - 4, 8, 8))
                else:
                    p.setBrush(QColor(255, 255, 255))
                    p.drawRect(QRectF(wpt.x() - 4, wpt.y() - 4, 8, 8))
            p.setBrush(Qt.NoBrush)
        # 拡大の中心
        if getattr(self, "pivot", None) is not None:
            px, py = self.pivot
            x, y = rect.x() + px * s, rect.y() + py * s
            p.setPen(QPen(QColor(255, 80, 200, 230), 2))
            p.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
            p.drawLine(QPointF(x, y - 9), QPointF(x, y + 9))
            p.drawEllipse(QPointF(x, y), 5, 5)
        # 選択枠
        for i in sorted(self.sel):
            if i >= len(self.infos):
                continue
            moving = self._drag is not None and (self.target == "all" or i == self.cur)
            off = self._off if moving else QPointF()
            if i == self.cur and self.target == "box" and self.infos[i].get("box") is not None:
                h, col = self.infos[i]["box"], QColor(255, 170, 60, 230)
            else:
                h = self.infos[i]["text"]
                col = QColor(80, 180, 255, 230) if i == self.cur else QColor(80, 180, 255, 120)
            h = h.translated(off)
            p.setPen(QPen(col, 1, Qt.DashLine))
            p.drawRect(QRectF(rect.x() + h.x() * s, rect.y() + h.y() * s, h.width() * s, h.height() * s))
        # 文字選択
        if (self.char_mode or self.char_sel) and self._drag is None and (self.char_boxes or self.empty_caret):
            xf = self._char_xf()
            if self.char_sel and self.char_sel[1] > self.char_sel[0]:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(60, 150, 255, 90))
                for i in range(self.char_sel[0], min(self.char_sel[1], len(self.char_boxes))):
                    r = self.char_boxes[i]
                    if r.width() <= 0:
                        continue
                    poly = [self._to_widget(xf.map(pt)) for pt in
                            (r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft())]
                    from PySide6.QtGui import QPolygonF
                    p.drawPolygon(QPolygonF(poly))
                p.setBrush(Qt.NoBrush)
            if self.char_mode and self.char_sel:
                k = self.char_sel[1] if self.char_sel[1] > self.char_sel[0] else self.char_sel[0]
                ln = self._caret_line(k)
                a_, b_ = ln if ln else (None, None)
                if a_ is not None:
                    p.setPen(QPen(QColor(60, 150, 255), 2))
                    wa, wb = self._to_widget(xf.map(a_)), self._to_widget(xf.map(b_))
                    p.drawLine(wa, wb)
                    if self.preedit:                  # 変換中の文字を、カーソルの位置に仮表示
                        hpx = max(10.0, (wb.y() - wa.y()) * 0.75)
                        fnt = QFont(self.font())
                        fnt.setPixelSize(int(hpx))
                        p.setFont(fnt)
                        fmw = p.fontMetrics()
                        tw = fmw.horizontalAdvance(self.preedit)
                        box = QRectF(wa.x(), wa.y(), tw + 6, wb.y() - wa.y())
                        p.fillRect(box, QColor(0, 0, 0, 170))
                        p.setPen(QColor(255, 230, 80))
                        p.drawText(box.adjusted(3, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, self.preedit)
                        p.drawLine(QPointF(box.left() + 3, box.bottom() - 3), QPointF(box.right() - 3, box.bottom() - 3))
        p.end()

    # ---- バウンディングボックス
    def _to_widget(self, c):
        rect, s = self._fit()
        return QPointF(rect.x() + c.x() * s, rect.y() + c.y() * s)

    def _xf_transform(self, pos, xf):
        from PySide6.QtGui import QTransform
        t = QTransform()
        t.translate(*pos)
        t.rotate(-float(xf.get("rot", 0)))
        t.scale(float(xf.get("sx", 100)) / 100, float(xf.get("sy", 100)) / 100)
        t.translate(-pos[0], -pos[1])
        return t

    def _handles(self):
        """現在のテロップのハンドル位置（キャンバス座標）"""
        if self.xf_ctx is None or self.cur >= len(self.infos) or len(self.sel) != 1:
            return None
        raw = self.infos[self.cur].get("raw")
        if raw is None or raw.isEmpty():
            return None
        pos, xf = self.xf_ctx
        t = self._xf_transform(pos, xf)
        l, r, tp, b = raw.left(), raw.right(), raw.top(), raw.bottom()
        mx, my = (l + r) / 2, (tp + b) / 2
        pts = {"tl": (l, tp), "tc": (mx, tp), "tr": (r, tp), "ml": (l, my), "mr": (r, my),
               "bl": (l, b), "bc": (mx, b), "br": (r, b)}
        hs = {k: t.map(QPointF(*v)) for k, v in pts.items()}
        # 回転ハンドル: 上辺の中央から外向きに少し離す
        _, s = self._fit()
        up = hs["tc"] - t.map(QPointF(mx, my))
        n = (up.x() ** 2 + up.y() ** 2) ** 0.5 or 1
        hs["rot"] = hs["tc"] + QPointF(up.x() / n, up.y() / n) * (28 / s)
        hs["anchor"] = QPointF(*pos)
        return hs

    def _hit_handle(self, wpos):
        hs = self._handles()
        if not hs:
            return None
        for k, c in hs.items():
            if k == "anchor":
                continue
            w = self._to_widget(c)
            if abs(w.x() - wpos.x()) <= 9 and abs(w.y() - wpos.y()) <= 9:
                return k
        return None

    def _handle_cursor(self, k):
        """ハンドルに合わせたカーソル（拡大縮小は向きのある矢印、回転は丸い矢印）"""
        if k == "rot":
            return _rotate_cursor()
        dirs = {"tl": (-1, -1), "tc": (0, -1), "tr": (1, -1), "ml": (-1, 0), "mr": (1, 0),
                "bl": (-1, 1), "bc": (0, 1), "br": (1, 1)}
        if k not in dirs:
            return QCursor(Qt.SizeAllCursor)
        rot = float((self.xf_ctx[1] if self.xf_ctx else {}).get("rot", 0))
        dx, dy = dirs[k]
        ang = (math.degrees(math.atan2(dy, dx)) - rot) % 180       # 0=横 90=縦（回転も考える）
        shapes = [Qt.SizeHorCursor, Qt.SizeFDiagCursor, Qt.SizeVerCursor, Qt.SizeBDiagCursor]
        return QCursor(shapes[int(((ang + 22.5) % 180) // 45)])

    def mousePressEvent(self, e):
        c = self._to_canvas(e.position())
        if self.char_mode and e.button() == Qt.LeftButton:
            if self._in_text(c):
                k = self._caret_at(c)
                if e.modifiers() & Qt.ShiftModifier and self.char_sel:
                    a0 = self.char_sel[0] if k >= self.char_sel[0] else self.char_sel[1]
                else:
                    a0 = k
                self._csel_anchor = a0
                self.set_char_sel((min(a0, k), max(a0, k)))
                self.setFocus()
                return
            self.exit_char_mode()
        if e.button() == Qt.LeftButton and not getattr(self, "pick_pivot", False):
            k = self._hit_handle(e.position())
            if k:
                pos, xf = self.xf_ctx
                raw = self.infos[self.cur]["raw"]
                pts = {"tl": (raw.left(), raw.top()), "tc": (raw.center().x(), raw.top()),
                       "tr": (raw.right(), raw.top()), "ml": (raw.left(), raw.center().y()),
                       "mr": (raw.right(), raw.center().y()), "bl": (raw.left(), raw.bottom()),
                       "bc": (raw.center().x(), raw.bottom()), "br": (raw.right(), raw.bottom())}
                hu = None if k == "rot" else (pts[k][0] - pos[0], pts[k][1] - pos[1])
                self.setCursor(self._handle_cursor(k))
                self._hdrag = {"k": k, "hu": hu, "rot0": float(xf.get("rot", 0)),
                               "sx0": float(xf.get("sx", 100)), "sy0": float(xf.get("sy", 100)),
                               "a0": math.atan2(c.y() - pos[1], c.x() - pos[0])}
                return
        if getattr(self, "pick_pivot", False) and e.button() == Qt.LeftButton:
            self.pick_pivot = False
            self.setCursor(Qt.ArrowCursor)
            self.pivotPicked.emit(c.x(), c.y())
            return
        hit = self._hit(c)
        self.setFocus()
        if e.button() != Qt.LeftButton or hit is None:
            return
        idx, part = hit
        additive = bool(e.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier | Qt.MetaModifier))
        if additive:
            self.picked.emit(idx, True)
            return
        if idx not in self.sel or len(self.sel) <= 1:
            self.picked.emit(idx, False)
        else:
            self.cur = idx
        # 複数選択中に文字をつかむ or ⌥ → テロップ丸ごと（座布団も一緒に）動かす
        if part == "text" and (len(self.sel) > 1 or e.modifiers() & Qt.AltModifier):
            self.target = "all"
        else:
            self.target = part
        self._drag = c
        self.setCursor(Qt.ClosedHandCursor)
        self._static, self._moving = None, self.image
        if self.split_fn:
            try:
                self._static, self._moving = self.split_fn(self.target)
            except Exception:
                traceback.print_exc()
        self.update()

    def mouseMoveEvent(self, e):
        c = self._to_canvas(e.position())
        if self._csel_anchor is not None:
            k = self._caret_at(c)
            a0 = self._csel_anchor
            self.set_char_sel((min(a0, k), max(a0, k)))
            return
        if self.char_mode and self._drag is None and self._hdrag is None:
            self.setCursor(Qt.IBeamCursor if self._in_text(c) else Qt.ArrowCursor)
            return
        if self._hdrag is not None:
            d = self._hdrag
            pos, xf = self.xf_ctx
            if d["k"] == "rot":
                a = math.atan2(c.y() - pos[1], c.x() - pos[0])
                rot = d["rot0"] - math.degrees(a - d["a0"])
                if e.modifiers() & Qt.ShiftModifier:
                    rot = round(rot / 15) * 15
                self.xfChanged.emit(rot, d["sx0"], d["sy0"])
                return
            from PySide6.QtGui import QTransform
            inv = QTransform()
            inv.rotate(d["rot0"])                          # 見た目の回転を戻して、テロップの縦横で測る
            v = inv.map(QPointF(c.x() - pos[0], c.y() - pos[1]))
            hx, hy = d["hu"]
            sx, sy = d["sx0"], d["sy0"]
            fx = v.x() / hx * 100 if abs(hx) > 1 else None
            fy = v.y() / hy * 100 if abs(hy) > 1 else None
            k = d["k"]
            if k in ("ml", "mr") and fx:
                sx = fx
            elif k in ("tc", "bc") and fy:
                sy = fy
            elif fx and fy:
                if self.keep_aspect() and not (e.modifiers() & Qt.ShiftModifier):
                    rel = ((fx / d["sx0"]) + (fy / d["sy0"])) / 2
                    sx, sy = d["sx0"] * rel, d["sy0"] * rel
                else:
                    sx, sy = fx, fy
            self.xfChanged.emit(d["rot0"], max(1.0, sx), max(1.0, sy))
            return
        if self._drag is None:
            k = self._hit_handle(e.position())
            if k:
                self.setCursor(self._handle_cursor(k))
                return
        if self._drag is not None:
            d = c - self._drag
            if e.modifiers() & Qt.ShiftModifier:      # Shiftで縦横どちらかに固定
                if abs(d.x()) > abs(d.y()):
                    d.setY(0)
                else:
                    d.setX(0)
            self._off = d
            self.update()
        else:
            self.setCursor(Qt.OpenHandCursor if self._hit(c) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e):
        if self._csel_anchor is not None:
            self._csel_anchor = None
            return
        if self._hdrag is not None:
            self._hdrag = None
            self.setCursor(Qt.ArrowCursor)
            self.update()
            return
        if self._drag is not None:
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)
            tgt = self.target
            if self.target == "all":
                self.target = "text"
            if self._off.manhattanLength() > 0:
                self.moved.emit(tgt, self._off.x(), self._off.y())
            else:
                self._off = QPointF()
            self.update()

    def keyPressEvent(self, e):
        if self.char_mode:
            n = len(self.char_boxes)
            sel = self.char_sel or (0, 0)
            if e.key() == Qt.Key_Escape:
                self.exit_char_mode()
                return
            if e.modifiers() & Qt.AltModifier and e.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                st = 10.0 if e.modifiers() & Qt.ShiftModifier else 1.0
                dk = {Qt.Key_Left: -st, Qt.Key_Right: st}.get(e.key(), 0.0)
                db = {Qt.Key_Up: st, Qt.Key_Down: -st}.get(e.key(), 0.0)
                self.charNudge.emit(dk, db)
                return
            if e.key() in (Qt.Key_Left, Qt.Key_Right):
                d = -1 if e.key() == Qt.Key_Left else 1
                if e.modifiers() & Qt.ShiftModifier:
                    a0 = getattr(self, "_kanchor", None)
                    if a0 is None or not (sel[0] <= a0 <= sel[1]):
                        a0 = sel[0]
                    self._kanchor = a0
                    head = sel[1] if sel[1] != a0 else sel[0]
                    head = max(0, min(n, head + d))
                    self.set_char_sel((min(a0, head), max(a0, head)))
                else:
                    self._kanchor = None
                    k = max(0, min(n, (sel[1] if d > 0 else sel[0]) + (d if sel[0] == sel[1] else 0)))
                    self.set_char_sel((k, k))
                return
            cmd = bool(e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier))
            if e.key() == Qt.Key_A and cmd:
                self.set_char_sel((0, n))
                return
            if cmd and e.key() in (Qt.Key_C, Qt.Key_X, Qt.Key_V):
                self.textCommand.emit({Qt.Key_C: "copy", Qt.Key_X: "cut", Qt.Key_V: "paste"}[e.key()])
                return
            if e.key() in (Qt.Key_Up, Qt.Key_Down):
                ln = self._caret_line(sel[1] if sel[1] > sel[0] else sel[0])
                if ln:
                    hgt = ln[1].y() - ln[0].y()
                    pt = QPointF(ln[0].x(), (ln[0].y() + ln[1].y()) / 2 + (hgt * 1.1 if e.key() == Qt.Key_Down else -hgt * 1.1))
                    k = self._caret_at(self._char_xf().map(pt))
                    if e.modifiers() & Qt.ShiftModifier:
                        a0 = sel[0]
                        self.set_char_sel((min(a0, k), max(a0, k)))
                    else:
                        self.set_char_sel((k, k))
                return
            if e.key() == Qt.Key_Backspace:
                self.textCommand.emit("backspace")
                return
            if e.key() == Qt.Key_Delete:
                self.textCommand.emit("delete")
                return
            if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not cmd:
                self.textInput.emit("\n")
                return
            if e.text() and not cmd and e.text().isprintable():
                self.textInput.emit(e.text())
                return
            return
        step = 10 if e.modifiers() & Qt.ShiftModifier else 1
        d = {Qt.Key_Left: (-step, 0), Qt.Key_Right: (step, 0),
             Qt.Key_Up: (0, -step), Qt.Key_Down: (0, step)}.get(e.key())
        if d:
            tgt = "all" if len(self.sel) > 1 else self.target
            self.moved.emit(tgt, *d)
        elif e.key() == Qt.Key_Tab and self.cur < len(self.free_box) and self.free_box[self.cur]:
            self.target = "box" if self.target == "text" else "text"
            self.update()
        else:
            super().keyPressEvent(e)

    def focusNextPrevChild(self, nxt):
        return False        # Tabキーを自分で使う


THUMB_PX = 160          # スタイル棚のタイル（正方形）の解像度


def _alpha_bounds(img):
    """画像の中で、実際に描かれている範囲（影や押し出しも含む）"""
    try:
        from PIL import Image as _PIL
        a = img.convertToFormat(QImage.Format_Alpha8)
        w, h, bpl = a.width(), a.height(), a.bytesPerLine()
        L = _PIL.frombuffer("L", (w, h), bytes(a.constBits())[: bpl * h], "raw", "L", bpl, 1)
        bb = L.point(lambda v: 255 if v > 8 else 0).getbbox()
        return QRectF(bb[0], bb[1], bb[2] - bb[0], bb[3] - bb[1]) if bb else None
    except Exception:
        return None


def _square(r):
    side = max(r.width(), r.height())
    c = r.center()
    return QRectF(c.x() - side / 2, c.y() - side / 2, side, side)


def thumb_title(style, sample, W=1000, H=1000, size=240):   # 影や押し出しが切れないよう広い画面で描く
    """スタイル見本用のタイトル（文字サイズに合わせて各数値を縮尺）"""
    t = model.apply_style(model.new_title(W, H), style)
    k = float(size) / max(1.0, float(t["font"]["size"]))
    t["font"]["size"] = size
    t["font"]["tracking"] = t["font"].get("tracking", 0) * k
    for e in t["edges"]:
        e["width"] = e["width"] * k
        e["soft"] = e.get("soft", 0) * k
    for key in ("distance", "blur"):
        t["shadow"][key] = t["shadow"][key] * k
    t["extrude"]["depth"] = max(1, int(t["extrude"]["depth"] * k))
    bx = t["box"]
    bx["mode"] = "fit"
    for key in ("padx", "pady", "radius"):
        bx[key] = bx[key] * k
    t["text"] = sample
    t["pos"] = [W / 2, H / 2]
    return t


# ------------------------------------------------------------ メインウィンドウ
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION} — {APP_TAGLINE}")
        self.resize(1560, 900)
        self.bridge = Bridge()
        self.settings = model.load_json(SETTINGS_PATH, {}) or {}
        if not self.settings.get("mode_v2"):          # 既定を「テロップごとに別」に
            self.settings["mode"] = "separate"
            self.settings["mode_v2"] = True
        if not self.settings.get("out_v2"):           # 既定の出力を Text+ に
            self.settings["output"] = "textplus"
            self.settings["out_v2"] = True
        self.styles = model.load_json(STYLES_PATH, {}) or {}
        self.index = model.load_json(INDEX_PATH, {}) or {}
        self.doc = model.new_doc(style=self.styles.get("__default__"))
        self.doc["mode"] = self.settings.get("mode", "separate")
        self.cur = 0
        self.sel = {0}
        self.editing = None       # {"gid","rev","folder","mode","mpis":{uid:mpi}}
        self.binds = []
        self.loading = False
        self.dirty = False
        self.last_infos = []
        self.last_seen = None
        self.thumb_cache = {}
        self.rcache = {}
        self.timer = QTimer(self, singleShot=True, interval=30, timeout=self.do_render)
        QApplication.instance().installEventFilter(self)
        self.watch = QTimer(self, interval=700, timeout=self._poll_selection)
        self._build()
        self._setup_scrub()
        self.load_ui()
        self._out_changed()
        self._localize_keys()
        self.do_render()
        self._apply_on_top(bool(self.settings.get("on_top", True)))

    def _localize_keys(self):
        """ボタンや説明のショートカット表記を Windows 用（Ctrl+）にする"""
        if not IS_WIN:
            return
        from PySide6.QtWidgets import QAbstractButton
        short = {"＋追加 ⌘T": ("＋追加", "Ctrl+T"), "複製 ⌘D": ("複製", "Ctrl+D")}   # 幅の狭いボタンは表記を短く
        for w in self.findChildren(QAbstractButton) + self.findChildren(QLabel):
            t = w.text()
            if t in short:
                w.setText(short[t][0])
                w.setToolTip(short[t][1])
            elif "⌘" in t or "⇧" in t:
                w.setText(keys(t))

    # 現在のテロップ
    @property
    def t(self):
        return self.doc["objects"][self.cur]

    @t.setter
    def t(self, v):
        self.doc["objects"][self.cur] = v

    # ---------- UI構築 ----------
    def _build(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        self.btn_grab = QPushButton("背景を取得 (⌘B)")
        self.btn_grab.clicked.connect(lambda: self.act_grab())
        self.cmb_bg = QComboBox()
        for label, key in [("Resolveの画", "resolve"), ("市松", "checker"), ("黒", "black"), ("グレー", "gray")]:
            self.cmb_bg.addItem(label, key)
        self.cmb_bg.currentIndexChanged.connect(self._bg_mode)
        self.chk_safe = QCheckBox("セーフエリア")
        self.chk_safe.setChecked(True)
        self.chk_safe.toggled.connect(lambda v: (setattr(self.preview, "safe", v), self.preview.update()))
        self.chk_top = QCheckBox("常に手前")
        self.chk_top.setChecked(bool(self.settings.get("on_top", True)))
        self.chk_top.toggled.connect(self._apply_on_top)
        self.chk_follow = QCheckBox("タイムラインの選択に追従")
        self.chk_follow.setToolTip("Resolveのタイムラインでタイトルをクリックすると、自動でこの画面に読み込みます")
        self.chk_follow.setChecked(bool(self.settings.get("follow", True)))
        self.chk_follow.toggled.connect(self._follow_toggled)
        btn_new = QPushButton("新規 (⌘N)")
        btn_new.clicked.connect(self.act_new)
        btn_open = QPushButton("開く…")
        btn_open.clicked.connect(self.act_open)
        btn_edit = QPushButton("選択タイトルを編集 (⌘L)")
        btn_edit.clicked.connect(self.act_edit_selected)
        btn_tp = QToolButton()
        btn_tp.setText("Text+を取り込む")
        btn_tp.setToolTip("タイムラインで選んだ Text+ / Fusionタイトルの文字と装飾を、今のタイトルに追加します\n"
                          "（▼から「新規として取り込む」も選べます。元のクリップは変更しません）")
        btn_tp.setPopupMode(QToolButton.MenuButtonPopup)
        from PySide6.QtWidgets import QMenu
        mtp = QMenu(btn_tp)
        mtp.addAction("今のタイトルに追加", lambda: self.act_import_textplus("append"))
        mtp.addAction("新規として取り込む", lambda: self.act_import_textplus("new"))
        btn_tp.setMenu(mtp)
        btn_tp.clicked.connect(lambda: self.act_import_textplus("append"))
        self.chk_autobg = QCheckBox("背景を自動更新")
        self.chk_autobg.setToolTip("Resolveで再生ヘッドを動かして止めると、その位置の画を自動で背景にします")
        self.chk_autobg.setChecked(bool(self.settings.get("autobg", True)))
        self.chk_autobg.toggled.connect(lambda v: self.settings.__setitem__("autobg", v))
        for w in (self.btn_grab, self.cmb_bg, self.chk_autobg, self.chk_safe, self.chk_top, self.chk_follow):
            top.addWidget(w)
        top.addStretch(1)
        for w in (btn_new, btn_open, btn_tp, btn_edit):
            top.addWidget(w)
        root.addLayout(top)

        split = QSplitter(Qt.Horizontal)

        # 左: テロップ一覧と整列
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel("テロップ（⇧/⌘クリックで複数選択）"))
        self.lst_objs = QListWidget()
        self.lst_objs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.lst_objs.itemSelectionChanged.connect(self._list_sel_changed)
        ll.addWidget(self.lst_objs, 1)
        g = QGridLayout()
        for i, (label, fn) in enumerate([("＋追加 ⌘T", self.obj_add), ("複製 ⌘D", self.obj_dup), ("削除", self.obj_del),
                                          ("▲上へ", lambda: self.obj_move(1)), ("▼下へ", lambda: self.obj_move(-1)),
                                          ("＋画像", self.obj_add_image)]):
            b = QPushButton(label)
            b.clicked.connect(fn)
            g.addWidget(b, i // 3, i % 3)
        ll.addLayout(g)

        al = QGroupBox("整列")
        agl = QVBoxLayout(al)
        ref = QHBoxLayout()
        ref.addWidget(QLabel("基準"))
        self.cmb_ref = QComboBox()
        self.cmb_ref.addItem("選択範囲", "sel")
        self.cmb_ref.addItem("画面", "frame")
        self.cmb_ref.addItem("セーフエリア(90%)", "safe")
        ref.addWidget(self.cmb_ref, 1)
        agl.addLayout(ref)
        ag = QGridLayout()
        buttons = [("左", "left"), ("左右中央", "hcenter"), ("右", "right"),
                   ("上", "top"), ("上下中央", "vcenter"), ("下", "bottom"),
                   ("横に等間隔", "hdist"), ("縦に等間隔", "vdist"), ("間隔を指定", "vgap")]
        for i, (label, key) in enumerate(buttons):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self.align(k))
            ag.addWidget(b, i // 3, i % 3)
        agl.addLayout(ag)
        gap = QHBoxLayout()
        gap.addWidget(QLabel("指定間隔(縦)"))
        self.sp_gap = spin(-500, 2000, 2, 0, " px")
        self.sp_gap.setValue(int(self.settings.get("gap", 20)))
        gap.addWidget(self.sp_gap)
        gap.addStretch(1)
        agl.addLayout(gap)
        ll.addWidget(al)
        left.setMinimumWidth(240)
        split.addWidget(left)

        # 中央: プレビュー
        self.preview = Preview()
        self.preview.moved.connect(self._moved)
        self.preview.picked.connect(self._picked)
        self.preview.split_fn = self._split_images
        self.preview.pivotPicked.connect(self._pivot_picked)
        self.preview.xfChanged.connect(self._xf_dragged)
        self.preview.charSelected.connect(self._preview_char_sel)
        self.preview.charNudge.connect(self._char_nudge)
        self.preview.textInput.connect(self._edit_insert)
        self.preview.textCommand.connect(self._edit_command)
        self.preview.newTextAt.connect(self._new_text_at)
        self.char_sel = None
        self._csync = False
        self.preview.keep_aspect = lambda: self.chk_keep.isChecked()
        center = QSplitter(Qt.Vertical)
        center.addWidget(self.preview)
        center.addWidget(self._style_strip())
        center.setStretchFactor(0, 1)
        center.setStretchFactor(1, 0)
        center.setSizes([740, 130])
        center.setChildrenCollapsible(False)
        split.addWidget(center)

        # 右: 設定
        tabs = QTabWidget()
        tabs.addTab(self._tab_text(), "文字")
        tabs.addTab(self._scroll(self._tab_face_edge()), "面/エッジ")
        tabs.addTab(self._scroll(self._tab_shadow()), "影/押出")
        tabs.addTab(self._scroll(self._tab_box()), "座布団")
        tabs.addTab(self._scroll(self._tab_anim()), "アニメ")
        tabs.addTab(self._scroll(self._tab_image()), "画像")
        self.tabs = tabs
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self.char_banner = QFrame()
        self.char_banner.setStyleSheet("QFrame{background:#ff9a2e; border-radius:5px;} QLabel{color:#1a1a1a;"
                                       " font-weight:bold; background:transparent;}")
        bl = QHBoxLayout(self.char_banner)
        bl.setContentsMargins(8, 4, 6, 4)
        self.lbl_char = QLabel("")
        self.lbl_char.setWordWrap(True)
        bl.addWidget(self.lbl_char, 1)
        b_clear = QPushButton("指定を消す")
        b_clear.setToolTip("選んだ文字の指定を消して、テロップ全体の設定に戻します")
        b_clear.clicked.connect(lambda: self._cs_apply("clear", None))
        bl.addWidget(b_clear)
        b_done = QPushButton("全体に戻る")
        b_done.setToolTip("文字の選択をやめて、テロップ全体の設定に戻ります（Esc）")
        b_done.clicked.connect(self._char_done)
        bl.addWidget(b_done)
        self.char_banner.hide()
        rl.addWidget(self.char_banner)
        rl.addWidget(tabs, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 1)
        split.setSizes([250, 840, 470])
        root.addWidget(split, 1)

        # 下部
        bot = QHBoxLayout()
        self.lbl_mode = QLabel("新規タイトル")
        self.lbl_mode.setStyleSheet("font-weight:bold;")
        bot.addWidget(self.lbl_mode)
        self.btn_pending = QPushButton("")
        self.btn_pending.setStyleSheet("QPushButton{background:#d68a2f;color:white;padding:4px 10px;border-radius:4px;}")
        self.btn_pending.clicked.connect(self._open_pending)
        self.btn_pending.hide()
        bot.addWidget(self.btn_pending)
        bot.addStretch(1)
        bot.addWidget(QLabel("配置"))
        self.cmb_mode = QComboBox()
        for label, key in MODES:
            self.cmb_mode.addItem(label, key)
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        bot.addWidget(self.cmb_mode)
        bot.addWidget(QLabel("出力"))
        self.cmb_out = QComboBox()
        self.cmb_out.addItem("Text+タイトル", "textplus")
        self.cmb_out.addItem("PNG画像", "png")
        self.cmb_out.setToolTip("Text+タイトル: 本物の Fusion タイトルとして配置（Inspector でも触れます。PNG は使いません）\n"
                                "PNG画像: 画像として配置")
        i = self.cmb_out.findData(self.settings.get("output", "textplus"))
        self.cmb_out.setCurrentIndex(max(0, i))
        self.cmb_out.currentIndexChanged.connect(self._out_changed)
        bot.addWidget(self.cmb_out)
        bot.addWidget(QLabel("尺"))
        self.sp_dur = spin(0.1, 600, 0.5, 1, " 秒")
        self.sp_dur.setValue(float(self.settings.get("duration", 5.0)))
        bot.addWidget(self.sp_dur)
        bot.addWidget(QLabel("トラック"))
        self.cmb_track = QComboBox()
        self.cmb_track.addItem("自動（映像の上の空き）", 0)
        for i in range(1, 21):
            self.cmb_track.addItem(f"V{i}", i)
        bot.addWidget(self.cmb_track)
        btn_png = QPushButton("PNG書き出しのみ…")
        btn_png.clicked.connect(self.act_export_png)
        bot.addWidget(btn_png)
        self.btn_newplace = QPushButton("新規として配置")
        self.btn_newplace.clicked.connect(self.act_place_new)
        self.btn_newplace.hide()
        bot.addWidget(self.btn_newplace)
        self.btn_place = QPushButton("Resolveに配置 (⌘↩)")
        self.btn_place.setStyleSheet("QPushButton{background:#2f6fd6;color:white;padding:6px 18px;border-radius:4px;font-weight:bold;}")
        self.btn_place.clicked.connect(self.act_place)
        bot.addWidget(self.btn_place)
        root.addLayout(bot)

        self.setCentralWidget(central)
        self.statusBar().showMessage("準備完了")

        # メニュー（ショートカットはここに集約。⌘Qで本当に終了）
        def add(menu, label, key, fn, role=None):
            a = QAction(label, self)
            if key:
                a.setShortcut(QKeySequence(key))
                a.setShortcutContext(Qt.ApplicationShortcut)
            if role is not None:
                a.setMenuRole(role)
            a.triggered.connect(lambda _=False: fn())
            menu.addAction(a)
            self.addAction(a)
            return a
        ed = self.menuBar().addMenu("編集")
        add(ed, "取り消す", "Ctrl+Z", self.act_undo)
        add(ed, "やり直す", "Ctrl+Shift+Z", self.act_redo)
        m = self.menuBar().addMenu("ファイル")
        add(m, "新規タイトル", "Ctrl+N", self.act_new)
        add(m, "開く（複製として）…", "Ctrl+O", self.act_open)
        add(m, "Text+を取り込む（今のタイトルに追加）", "Ctrl+I", lambda: self.act_import_textplus("append"))
        add(m, "Text+を取り込む（新規）", "Ctrl+Shift+I", lambda: self.act_import_textplus("new"))
        m.addSeparator()
        add(m, "選択タイトルを編集", "Ctrl+L", self.act_edit_selected)
        add(m, "背景を取得", "Ctrl+B", self.act_grab)
        add(m, "Resolveに配置／更新", "Ctrl+Return", self.act_place)
        m.addSeparator()
        add(m, "ウィンドウを隠す", "Ctrl+W", self.close)
        add(m, "CT Titler を終了", "Ctrl+Q", self.quit_app, QAction.QuitRole)
        e = self.menuBar().addMenu("テロップ")
        add(e, "テロップを追加", "Ctrl+T", self.obj_add)
        add(e, "テロップを複製", "Ctrl+D", self.obj_dup)
        add(e, "画像を追加…", None, self.obj_add_image)
        add(e, "テロップを削除", None, self.obj_del)

    # ---------- 画像のテロップ ----------
    def _tab_image(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.lbl_img = QLabel("")
        self.lbl_img.setWordWrap(True)
        lay.addWidget(self.lbl_img)
        row = QHBoxLayout()
        b = QPushButton("画像を差し替え…")
        b.clicked.connect(self.img_replace)
        row.addWidget(b)
        b = QPushButton("原寸（100%）に戻す")
        b.clicked.connect(self.img_actual)
        row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        form = QFormLayout()
        pr = QHBoxLayout()
        pr.addWidget(QLabel("X"))
        pr.addWidget(self._bind(spin(-10000, 10000, 1, 1), ("pos", 0), "float"))
        pr.addWidget(QLabel("Y"))
        pr.addWidget(self._bind(spin(-10000, 10000, 1, 1), ("pos", 1), "float"))
        form.addRow("位置（中心）", pr)
        form.addRow("大きさ", self._bind(spin(1, 2000, 1, 1, " %"), ("xf", "sx"), "float"))
        form.addRow("回転", self._bind(spin(-3600, 3600, 1, 1, " °"), ("xf", "rot"), "float"))
        form.addRow("不透明度", self._bind(spin(0, 100, 1, 0, " %"), ("image", "opacity"), "pct"))
        self.cmb_follow = QComboBox()
        self.cmb_follow.currentIndexChanged.connect(self._follow_changed)
        form.addRow("いっしょに流す", self.cmb_follow)
        lay.addLayout(form)
        lay.addWidget(QLabel("ロゴや帯・飾りの画像を置けます。テロップ一覧の▲▼で重なり順を変えると、\n"
                             "文字の後ろに敷く帯にもできます。「いっしょに流す」でロール／クロールの\n"
                             "テロップを選ぶと、その文字と同じ速さで流れます（位置は流す前の配置で決めます）。"))
        lay.addStretch(1)
        return w

    def _image_ui(self):
        """画像のテロップを選んでいるときは、文字用のタブを止めて「画像」タブを使う"""
        if not hasattr(self, "tabs"):
            return
        img = R.is_image(self.t)
        cur = self.tabs.currentIndex()
        if img and cur < 4:                     # 先に移ってから止める（止めると Qt が勝手に隣のタブへ移るため）
            self.tabs.setTabEnabled(5, True)
            self.tabs.setCurrentIndex(5)
        if not img and cur == 5:
            self.tabs.setTabEnabled(0, True)
            self.tabs.setCurrentIndex(0)
        for i in range(4):
            self.tabs.setTabEnabled(i, not img)
        self.tabs.setTabEnabled(5, img)
        self.grp_roll.setVisible(not img)
        if not img:
            return
        im = self.t.get("image") or {}
        ok = os.path.isfile(im.get("file", ""))
        self.lbl_img.setText(f"<b>{im.get('name', '')}</b>　{im.get('w')}×{im.get('h')} px"
                             + ("" if ok else "<br><span style='color:#d33'>画像ファイルが見つかりません</span>"))
        self.cmb_follow.blockSignals(True)
        self.cmb_follow.clear()
        self.cmb_follow.addItem("流さない", "")
        for o in self.doc["objects"]:
            if not R.is_image(o) and RL.active(o):
                kind = {"up": "ロール", "left": "クロール ←", "right": "クロール →"}[RL.get(o)["dir"]]
                self.cmb_follow.addItem(f"{obj_label(o)}（{kind}）", o["uid"])
        i = self.cmb_follow.findData(self.t.get("follow", ""))
        self.cmb_follow.setCurrentIndex(max(0, i))
        self.cmb_follow.blockSignals(False)

    def _follow_changed(self, i):
        if self.loading or not R.is_image(self.t):
            return
        self.t["follow"] = self.cmb_follow.currentData() or ""
        self.schedule()

    def _pick_image(self):
        start = self.settings.get("img_dir") or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(self, "画像を選ぶ", start,
                                              "画像 (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.psd)")
        if not path:
            return None
        self.settings["img_dir"] = os.path.dirname(path)
        img = QImage(path)
        if img.isNull():
            QMessageBox.warning(self, "画像", "この画像は読み込めませんでした。")
            return None
        # CT Titler の画像フォルダへコピー（元の画像を動かしてもタイトルが切れないように）。透明部分を保つ PNG で
        # （Resolve の Fusion は、ここで書いた TIFF を読むと画面全体が黒くなったため PNG にしている）
        folder = os.path.join(SUPPORT, "images")
        os.makedirs(folder, exist_ok=True)
        base = re.sub(r'[\\/:*?"<>|]', "_", os.path.splitext(os.path.basename(path))[0])[:40] or "image"
        dst = os.path.join(folder, f"{base}_{model.new_uid()}_ct.png")   # 末尾を数字にしない（連番と間違われない）
        img.convertToFormat(QImage.Format_ARGB32).save(dst, "PNG")
        return dst, img.width(), img.height(), os.path.basename(path)

    def obj_add_image(self):
        r = self._pick_image()
        if not r:
            return
        dst, w, h, name = r
        W, H = self.doc["canvas"]
        o = model.new_image(W, H, dst, w, h, name)
        self.doc["objects"].insert(self.cur + 1, o)
        self.cur += 1
        self.sel = {self.cur}
        self.load_ui()
        self.schedule()

    def img_replace(self):
        if not R.is_image(self.t):
            return
        r = self._pick_image()
        if not r:
            return
        dst, w, h, name = r
        self.t["image"].update({"file": dst, "w": int(w), "h": int(h), "name": name})
        self.load_ui()
        self.schedule()

    def img_actual(self):
        if R.is_image(self.t):
            self.t["xf"].update({"sx": 100.0, "sy": 100.0})
            self.load_ui()
            self.schedule()

    def _scroll(self, w):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for lab in w.findChildren(QLabel):      # 長い説明文は折り返す
            if len(lab.text()) > 16:
                lab.setWordWrap(True)
        sa.setWidget(w)
        return sa

    def _tab_text(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.txt = StyledTextEdit()
        self.txt.copy_hook = lambda a, b: json.dumps(
            dict(zip(("text", "styles"), self._clip_styled(a, b))), ensure_ascii=False).encode("utf-8")
        self.txt.paste_hook = self._txt_paste_styled
        self.txt.setPlaceholderText("ここに文字を入力（改行で複数行）")
        f = self.txt.font()
        f.setPointSize(16)
        self.txt.setFont(f)
        self.txt.setMinimumHeight(110)
        self.txt.textChanged.connect(self._text_changed)
        self.txt.selectionChanged.connect(self._txt_sel_changed)
        lay.addWidget(self.txt)
        hint = QLabel("プレビューの文字をダブルクリック（またはここで文字を選択）すると、"
                      "その文字だけフォント・サイズ・色・縁・影を変えられます")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#888; font-size:11px;")
        lay.addWidget(hint)

        form = QFormLayout()
        self.cmb_font = FontCombo()
        self.cmb_font.setItemDelegate(FontPreviewDelegate(self._font_sample, self.cmb_font))
        self.cmb_font.view().setMinimumWidth(480)
        self.cmb_font.setMaxVisibleItems(16)
        self.cmb_font.currentFontChanged.connect(self._family_changed)
        self.cmb_font.highlighted.connect(self._font_hover)
        self.cmb_font.popupClosed.connect(self._font_hover_end)
        self._font_orig = None
        form.addRow("フォント", self.cmb_font)
        self.cmb_style = QComboBox()
        self.cmb_style.currentTextChanged.connect(self._style_changed)
        form.addRow("ウェイト", self.cmb_style)
        szrow = QHBoxLayout()
        self.sp_size = self._bind(spin(4, 2000, 1, 0, " px"), ("font", "size"), "int")
        self.sp_size.setKeyboardTracking(False)          # 打ち終わってから反映（途中の数字で縮尺しない）
        szrow.addWidget(self.sp_size, 1)
        self.chk_scale_deco = QCheckBox("装飾も連動")
        self.chk_scale_deco.setToolTip("サイズを変えたとき、縁・影・押し出し・座布団・字間も同じ比率で変えます")
        self.chk_scale_deco.setChecked(bool(self.settings.get("scale_deco", True)))
        self.chk_scale_deco.toggled.connect(lambda v: self.settings.__setitem__("scale_deco", bool(v)))
        szrow.addWidget(self.chk_scale_deco)
        form.addRow("サイズ", szrow)
        trow = QHBoxLayout()
        trow.addWidget(self._bind(spin(-200, 500, 1, 1, " px"), ("font", "tracking"), "float"), 1)
        cgap = QCheckBox("縁の太さに合わせて自動で空ける")
        cgap.setToolTip("文字ごとの指定で縁が太い文字は、隣の文字にかぶらないよう、縁の太さの差の分だけ自動で間を空けます。\n"
                        "スタイルを当て直したり縁の太さを変えても、空きが自動で付いてくるので、手でのカーニングが要りません。")
        trow.addWidget(self._bind(cgap, ("font", "edge_gap"), "bool"))
        form.addRow("字間", trow)
        # 選んだ文字だけ: カーニング（その文字の後ろ）とベースライン（上下のずらし）
        krow = QHBoxLayout()
        self.sp_kern = spin(-500, 500, 1, 1, " px")
        self.sp_kern.setToolTip("選んだ文字の後ろの間隔を詰める（−）／空ける（＋）\n"
                                "プレビューの文字選択中は ⌥←→ でも調整（⇧で10px）")
        self.sp_kern.valueChanged.connect(lambda v: self._cs_apply("merge", {"kern": round(float(v), 2) or None}))
        krow.addWidget(self.sp_kern, 1)
        krow.addWidget(QLabel("ベースライン"))
        self.sp_bs = spin(-1000, 1000, 1, 1, " px")
        self.sp_bs.setToolTip("選んだ文字を上（＋）／下（−）にずらす。上付き・下付きにも\n"
                              "プレビューの文字選択中は ⌥↑↓ でも調整（⇧で10px）")
        self.sp_bs.valueChanged.connect(lambda v: self._cs_apply("merge", {"bs": round(float(v), 2) or None}))
        krow.addWidget(self.sp_bs, 1)
        self.lbl_kern = QLabel("カーニング")
        form.addRow(self.lbl_kern, krow)
        form.addRow("行間", self._bind(spin(0.5, 4.0, 0.05, 2, " 倍"), ("font", "leading"), "float"))

        al = QHBoxLayout()
        self.align_group = QButtonGroup(self)
        for label, key in [("左", "left"), ("中央", "center"), ("右", "right")]:
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.setProperty("key", key)
            self.align_group.addButton(b)
            al.addWidget(b)
        self.align_group.buttonClicked.connect(self._align_changed)
        al.addStretch(1)
        form.addRow("行揃え", al)

        pos = QHBoxLayout()
        self.sp_x = self._bind(spin(-10000, 10000, 1, 1), ("pos", 0), "float")
        self.sp_y = self._bind(spin(-10000, 10000, 1, 1), ("pos", 1), "float")
        pos.addWidget(QLabel("X"))
        pos.addWidget(self.sp_x)
        pos.addWidget(QLabel("Y"))
        pos.addWidget(self.sp_y)
        form.addRow("位置", pos)
        quick = QHBoxLayout()
        for label, fx, fy in [("中央", 0.5, 0.5), ("下", 0.5, 0.83), ("上", 0.5, 0.15)]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, fx=fx, fy=fy: self._set_pos(fx, fy))
            quick.addWidget(b)
        hc = QPushButton("左右中央")
        hc.clicked.connect(lambda: self._set_pos(0.5, None))
        quick.addWidget(hc)
        form.addRow("", quick)
        # アンカー: 3×3 のマス（よく使う9点）＋ 横・縦の数値（-1＝左/上, 0＝中央, 1＝右/下）
        anc = QGridLayout()
        anc.setSpacing(2)
        self.anc_cells = []
        for i in range(9):
            hx, vy = (i % 3) - 1.0, (i // 3) - 1.0
            b = QToolButton()
            b.setFixedSize(22, 22)
            b.setText("・")
            b.clicked.connect(lambda _=False, hx=hx, vy=vy: self._anchor_set_both(hx, vy))
            anc.addWidget(b, i // 3, i % 3)
            self.anc_cells.append((b, hx, vy))
        nums = QGridLayout()
        nums.setHorizontalSpacing(6)
        self.anc_w = {}
        for r_, (axis, title, lo, hi) in enumerate((("h", "横", "左", "右"), ("v", "縦", "上", "下"))):
            sl = QSlider(Qt.Horizontal)
            sl.setRange(-100, 100)
            sl.setMinimumWidth(80)
            sl.setToolTip(f"-1＝{lo}　0＝中央　1＝{hi}")
            sl.valueChanged.connect(lambda v, ax=axis: self._anchor_set(ax, v / 100.0))
            sp = QDoubleSpinBox()
            sp.setRange(-1.0, 1.0)
            sp.setSingleStep(0.05)
            sp.setDecimals(2)
            sp.setToolTip(f"-1＝{lo}　0＝中央　1＝{hi}")
            sp.valueChanged.connect(lambda v, ax=axis: self._anchor_set(ax, v))
            nums.addWidget(QLabel(title), r_, 0)
            nums.addWidget(sl, r_, 1)
            nums.addWidget(sp, r_, 2)
            self.anc_w[axis] = (sl, sp)
        nums.setColumnStretch(1, 1)
        ancw = QHBoxLayout()
        ancw.addLayout(anc)
        ancw.addSpacing(8)
        ancw.addLayout(nums, 1)
        form.addRow("アンカー", ancw)
        form.addRow("", QLabel("位置・回転・拡大の基準点（-1＝左/上、0＝中央、1＝右/下）"))
        rot = QHBoxLayout()
        rot.addWidget(self._bind(spin(-3600, 3600, 1, 1, " °"), ("xf", "rot"), "float"))
        rot.addWidget(QLabel("（＋で反時計回り）"))
        rot.addStretch(1)
        form.addRow("回転", rot)
        sc = QHBoxLayout()
        self.sp_sx = self._bind(spin(1, 2000, 1, 1, " %"), ("xf", "sx"), "float")
        self.sp_sy = self._bind(spin(1, 2000, 1, 1, " %"), ("xf", "sy"), "float")
        self.chk_keep = QCheckBox("縦横比を保つ")
        self.chk_keep.setChecked(True)
        self.sp_sx.valueChanged.connect(lambda v: self._keep_aspect("sx", v))
        self.sp_sy.valueChanged.connect(lambda v: self._keep_aspect("sy", v))
        sc.addWidget(QLabel("横"))
        sc.addWidget(self.sp_sx)
        sc.addWidget(QLabel("縦"))
        sc.addWidget(self.sp_sy)
        form.addRow("拡大", sc)
        sc2 = QHBoxLayout()
        sc2.addWidget(self.chk_keep)
        b = QPushButton("回転・拡大をリセット")
        b.clicked.connect(self._reset_xf)
        sc2.addWidget(b)
        sc2.addStretch(1)
        form.addRow("", sc2)
        lay.addLayout(form)
        lay.addStretch(1)
        return w

    def _tab_face_edge(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("フェース（文字の面）")
        g.setCheckable(True)
        self._bind(g, ("face", "enabled"), "bool")
        f = QFormLayout(g)
        f.addRow("色", self._bind(ColorButton(), ("face", "color"), "color"))
        f.addRow("不透明度", self._bind(spin(0, 100, 5, 0, " %"), ("face", "opacity"), "pct"))
        lay.addWidget(g)

        # 多色グラデーション
        gg = QGroupBox("グラデーション（オンにすると上の「色」の代わりに使います）")
        gg.setCheckable(True)
        self._bind(gg, ("face", "gradient"), "bool")
        gl = QVBoxLayout(gg)
        self.grad = GradientEditor()
        self.grad.changed.connect(self._grad_changed)
        self.grad.selectionChanged.connect(self._grad_sel_changed)
        gl.addWidget(self.grad)
        hint = QLabel("クリックで色を追加／ドラッグで移動／ダブルクリックで色変更／下へ引き離すか Delete で削除")
        hint.setStyleSheet("color:#888;font-size:11px;")
        gl.addWidget(hint)
        row = QHBoxLayout()
        row.addWidget(QLabel("選択中の色"))
        self.grad_color = ColorButton()
        self.grad_color.changed.connect(lambda: self.grad.set_selected(color=self.grad_color.color()))
        row.addWidget(self.grad_color)
        row.addWidget(QLabel("位置"))
        self.grad_pos = spin(0, 100, 1, 1, " %")
        self.grad_pos.valueChanged.connect(lambda v: None if self.loading else self.grad.set_selected(pos=v / 100))
        row.addWidget(self.grad_pos)
        row.addStretch(1)
        gl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("不透明"))
        self.grad_alpha = spin(0, 100, 5, 0, "%")
        self.grad_alpha.valueChanged.connect(lambda v: None if self.loading else self.grad.set_selected(alpha=v / 100))
        row.addWidget(self.grad_alpha)
        b = QPushButton("この色を削除")
        b.clicked.connect(self.grad.remove_selected)
        row.addWidget(b)
        row.addStretch(1)
        gl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("方向"))
        row.addWidget(self._bind(spin(-360, 360, 15, 0, " °"), ("face", "angle"), "int"))
        for label, ang in [("↓", 90), ("→", 0), ("↘", 45)]:
            bb = QToolButton()
            bb.setText(label)
            bb.clicked.connect(lambda _=False, a=ang: self._grad_angle(a))
            row.addWidget(bb)
        self.cmb_span = QComboBox()
        self.cmb_span.addItem("行ごと", "line")
        self.cmb_span.addItem("文字全体", "block")
        self.cmb_span.currentIndexChanged.connect(self._grad_span)
        row.addWidget(self.cmb_span)
        b = QPushButton("反転")
        b.clicked.connect(self.grad.reverse)
        row.addWidget(b)
        row.addStretch(1)
        gl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("プリセット"))
        self.cmb_gpreset = QComboBox()
        self.cmb_gpreset.addItem("選んで適用…", None)
        for name in model.GRADIENT_PRESETS:
            self.cmb_gpreset.addItem(name, name)
        self.cmb_gpreset.activated.connect(self._grad_preset)
        row.addWidget(self.cmb_gpreset, 1)
        gl.addLayout(row)
        lay.addWidget(gg)

        # 画像で塗る
        ig = QGroupBox("画像で塗る（オンにすると色・グラデーションの代わりに使います）")
        ig.setCheckable(True)
        self._bind(ig, ("face", "img_on"), "bool")
        il = QFormLayout(ig)
        ir = QHBoxLayout()
        b = QPushButton("画像を選ぶ…")
        b.clicked.connect(self._face_img_pick)
        ir.addWidget(b)
        self.lbl_face_img = QLabel("（未選択）")
        ir.addWidget(self.lbl_face_img, 1)
        il.addRow(ir)
        c = QComboBox()
        for label, key in R.FACE_FITS:
            c.addItem(label, key)
        il.addRow("当て方", self._bind(c, ("face", "img", "fit"), "combo"))
        c = QComboBox()
        c.addItem("文字全体に1枚", "block")
        c.addItem("行ごとに1枚", "line")
        il.addRow("範囲", self._bind(c, ("face", "img", "span"), "combo"))
        il.addRow("大きさ", self._bind(spin(10, 1000, 5, 0, " %"), ("face", "img", "scale"), "float"))
        lay.addWidget(ig)

        # 面のエンボス
        eg_ = QGroupBox("エンボス（面に立体感）")
        eg_.setCheckable(True)
        self._bind(eg_, ("face", "emboss", "on"), "bool")
        ef = QFormLayout(eg_)
        ef.addRow("強さ", self._bind(spin(0, 10, 0.1, 1), ("face", "emboss", "depth"), "float"))
        ef.addRow("幅（ぼかし）", self._bind(spin(0, 100, 0.5, 1, " px"), ("face", "emboss", "blur"), "float"))
        ef.addRow("光の向き", self._bind(spin(-360, 360, 15, 0, " °"), ("face", "emboss", "angle"), "int"))
        ef.addRow("", QLabel("光の向き: 225 = 左上から / 315 = 右上から / 90 = 下から"))
        lay.addWidget(eg_)

        self.edge_grads = {}
        for i in range(model.MAX_EDGES):
            g = QGroupBox(f"エッジ {i + 1}" + ("（文字に一番近い）" if i == 0 else ""))
            g.setCheckable(True)
            self._bind(g, ("edges", i, "enabled"), "bool")
            gl = QGridLayout(g)
            gl.addWidget(QLabel("太さ"), 0, 0)
            gl.addWidget(self._bind(spin(0, 300, 1, 1, " px"), ("edges", i, "width"), "float"), 0, 1)
            gl.addWidget(QLabel("色"), 0, 2)
            gl.addWidget(self._bind(ColorButton(), ("edges", i, "color"), "color"), 0, 3)
            gl.addWidget(QLabel("ソフト"), 1, 0)
            gl.addWidget(self._bind(spin(0, 200, 0.5, 1, " px"), ("edges", i, "soft"), "float"), 1, 1)
            gl.addWidget(QLabel("不透明"), 1, 2)
            gl.addWidget(self._bind(spin(0, 100, 5, 0, "%"), ("edges", i, "opacity"), "pct"), 1, 3)
            # 縁のグラデーション
            eg = QCheckBox("グラデーション（色の代わりに使う）")
            gl.addWidget(self._bind(eg, ("edges", i, "gradient"), "bool"), 2, 0, 1, 4)
            ged = GradientEditor()
            ged.setMinimumHeight(44)
            ged.changed.connect(lambda i=i: self._edge_grad_changed(i))
            ged.setToolTip("クリックで色を追加／ドラッグで移動／ダブルクリックで色変更／下へ引き離すか Delete で削除")
            self.edge_grads[i] = ged
            gl.addWidget(ged, 3, 0, 1, 4)
            er = QHBoxLayout()
            er.addWidget(QLabel("方向"))
            er.addWidget(self._bind(spin(-360, 360, 15, 0, " °"), ("edges", i, "angle"), "int"))
            c = QComboBox()
            c.addItem("行ごと", "line")
            c.addItem("文字全体", "block")
            er.addWidget(self._bind(c, ("edges", i, "span"), "combo"))
            b = QPushButton("反転")
            b.clicked.connect(ged.reverse)
            er.addWidget(b)
            er.addStretch(1)
            gl.addLayout(er, 4, 0, 1, 4)
            lay.addWidget(g)
        lay.addStretch(1)
        return w

    def _tab_shadow(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("ドロップシャドウ")
        g.setCheckable(True)
        self._bind(g, ("shadow", "enabled"), "bool")
        f = QFormLayout(g)
        f.addRow("色", self._bind(ColorButton(), ("shadow", "color"), "color"))
        f.addRow("不透明度", self._bind(spin(0, 100, 5, 0, " %"), ("shadow", "opacity"), "pct"))
        f.addRow("距離", self._bind(spin(0, 500, 1, 1, " px"), ("shadow", "distance"), "float"))
        f.addRow("方向", self._bind(spin(-360, 360, 5, 0, " °"), ("shadow", "angle"), "int"))
        f.addRow("ぼかし", self._bind(spin(0, 200, 1, 1, " px"), ("shadow", "blur"), "float"))
        f.addRow("", QLabel("方向: 0=右 / 90=下 / 180=左 / 270=上"))
        lay.addWidget(g)

        g = QGroupBox("押し出し（疑似3D）")
        g.setCheckable(True)
        self._bind(g, ("extrude", "enabled"), "bool")
        f = QFormLayout(g)
        f.addRow("奥行き", self._bind(spin(1, 400, 1, 1, " px"), ("extrude", "depth"), "float"))
        f.addRow("方向", self._bind(spin(-360, 360, 5, 0, " °"), ("extrude", "angle"), "int"))
        f.addRow("色（手前）", self._bind(ColorButton(), ("extrude", "color"), "color"))
        gr = QHBoxLayout()
        gr.addWidget(self._bind(QCheckBox("グラデーション　奥の色"), ("extrude", "grad"), "bool"))
        gr.addWidget(self._bind(ColorButton(), ("extrude", "color2"), "color"))
        gr.addStretch(1)
        f.addRow("", gr)
        f.addRow("不透明度", self._bind(spin(0, 100, 5, 0, " %"), ("extrude", "opacity"), "pct"))
        lay.addWidget(g)

        g = QGroupBox("カットアウトシャドー（文字の内側の影）")
        g.setCheckable(True)
        self._bind(g, ("inner", "enabled"), "bool")
        f = QFormLayout(g)
        f.addRow("色", self._bind(ColorButton(), ("inner", "color"), "color"))
        f.addRow("不透明度", self._bind(spin(0, 100, 5, 0, " %"), ("inner", "opacity"), "pct"))
        f.addRow("距離", self._bind(spin(0, 200, 1, 1, " px"), ("inner", "distance"), "float"))
        f.addRow("方向", self._bind(spin(-360, 360, 5, 0, " °"), ("inner", "angle"), "int"))
        f.addRow("ぼかし", self._bind(spin(0, 100, 0.5, 1, " px"), ("inner", "blur"), "float"))
        f.addRow("", QLabel("方向: 45 = 左上の内側に影が出る（文字がくぼんで見える）"))
        lay.addWidget(g)
        lay.addStretch(1)
        return w

    def _tab_box(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        g = QGroupBox("座布団（文字の背面ボックス）")
        g.setCheckable(True)
        self._bind(g, ("box", "enabled"), "bool")
        gl = QVBoxLayout(g)
        f = QFormLayout()
        f.addRow("色", self._bind(ColorButton(), ("box", "color"), "color"))
        f.addRow("不透明度", self._bind(spin(0, 100, 5, 0, " %"), ("box", "opacity"), "pct"))
        f.addRow("角丸", self._bind(spin(0, 500, 1, 1, " px"), ("box", "radius"), "float"))
        gl.addLayout(f)

        mode = QHBoxLayout()
        self.rb_fit = QRadioButton("文字に合わせる")
        self.rb_free = QRadioButton("位置・サイズを固定")
        self.rb_fit.toggled.connect(self._box_mode)
        mode.addWidget(self.rb_fit)
        mode.addWidget(self.rb_free)
        mode.addStretch(1)
        gl.addLayout(mode)

        self.box_fit_w = QWidget()
        ff = QFormLayout(self.box_fit_w)
        ff.setContentsMargins(0, 0, 0, 0)
        ff.addRow("余白 左右", self._bind(spin(0, 1000, 2, 1, " px"), ("box", "padx"), "float"))
        ff.addRow("余白 上下", self._bind(spin(0, 1000, 2, 1, " px"), ("box", "pady"), "float"))
        gl.addWidget(self.box_fit_w)

        self.box_free_w = QWidget()
        fl = QVBoxLayout(self.box_free_w)
        fl.setContentsMargins(0, 0, 0, 0)
        fr = QFormLayout()
        xy = QHBoxLayout()
        self.sp_bx = self._bind(spin(-10000, 10000, 1, 1), ("box", "rect", 0), "float")
        self.sp_by = self._bind(spin(-10000, 10000, 1, 1), ("box", "rect", 1), "float")
        xy.addWidget(QLabel("X"))
        xy.addWidget(self.sp_bx)
        xy.addWidget(QLabel("Y"))
        xy.addWidget(self.sp_by)
        fr.addRow("中心", xy)
        wh = QHBoxLayout()
        wh.addWidget(QLabel("幅"))
        wh.addWidget(self._bind(spin(1, 20000, 2, 0), ("box", "rect", 2), "float"))
        wh.addWidget(QLabel("高"))
        wh.addWidget(self._bind(spin(1, 20000, 2, 0), ("box", "rect", 3), "float"))
        fr.addRow("サイズ", wh)
        fl.addLayout(fr)
        row = QHBoxLayout()
        for label, fn in [("左右中央へ", lambda: self._box_place("hcenter")),
                          ("横幅いっぱい", lambda: self._box_place("full")),
                          ("今の文字に合わせ直す", lambda: self._box_place("fit"))]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        fl.addLayout(row)
        fl.addWidget(QLabel("文字を座布団の中で："))
        row = QHBoxLayout()
        for label, key in [("左寄せ", "left"), ("中央", "center"), ("右寄せ", "right"), ("上下中央", "vcenter")]:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, k=key: self._text_in_box(k))
            row.addWidget(b)
        fl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("寄せの余白"))
        self.sp_inset = spin(0, 2000, 2, 0, " px")
        self.sp_inset.setValue(int(self.settings.get("box_inset", 40)))
        row.addWidget(self.sp_inset)
        row.addStretch(1)
        fl.addLayout(row)
        fl.addWidget(QLabel("プレビュー上で座布団をドラッグして移動できます（文字の外側をつかむ）。\n"
                            "Tabキーで矢印キーの移動対象を 文字⇔座布団 で切り替え。\n"
                            "⌥を押しながら文字をドラッグすると座布団ごと動きます。"))
        gl.addWidget(self.box_free_w)
        lay.addWidget(g)
        lay.addStretch(1)
        return w

    def _tab_anim(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        # ロール（縦に流す）／クロール（横に流す）
        g = QGroupBox("ロール／クロール（エンドロールやニュースのように流す）")
        self.grp_roll = g
        g.setCheckable(True)
        self._bind(g, ("roll", "enabled"), "bool")
        f = QFormLayout(g)
        c = QComboBox()
        for label, key in RL.DIR_MODES:
            c.addItem(label, key)
        f.addRow("流し方", self._bind(c, ("roll", "dir"), "combo"))
        c = QComboBox()
        for label, key in RL.REPEAT_MODES:
            c.addItem(label, key)
        f.addRow("くり返し", self._bind(c, ("roll", "repeat"), "combo"))
        c = QComboBox()
        for label, key in RL.START_MODES:
            c.addItem(label, key)
        self.cmb_roll_start = c
        f.addRow("始まり", self._bind(c, ("roll", "start"), "combo"))
        c = QComboBox()
        for label, key in RL.END_MODES:
            c.addItem(label, key)
        self.cmb_roll_end = c
        f.addRow("終わり", self._bind(c, ("roll", "end"), "combo"))
        c = QComboBox()
        for label, key in RL.SPEED_MODES:
            c.addItem(label, key)
        f.addRow("速さ", self._bind(c, ("roll", "speed"), "combo"))
        f.addRow("1フレームあたり", self._bind(spin(1, 200, 1, 0, " px"), ("roll", "ppf"), "int"))
        f.addRow("", self._bind(QCheckBox("1フレームの移動量を整数ピクセルにする（ちらつき防止）"), ("roll", "snap"), "bool"))
        self.lbl_roll = QLabel("")
        self.lbl_roll.setWordWrap(True)
        f.addRow(self.lbl_roll)
        sr = QHBoxLayout()
        sr.addWidget(QLabel("確認"))
        self.sl_roll = QSlider(Qt.Horizontal)
        self.sl_roll.setRange(0, 1000)
        self.sl_roll.setToolTip("ドラッグすると、その時点の位置をプレビューで確かめられます")
        self.sl_roll.valueChanged.connect(self._roll_scrub)
        self.sl_roll.sliderReleased.connect(self._roll_scrub_end)
        sr.addWidget(self.sl_roll, 1)
        f.addRow(sr)
        lay.addWidget(g)
        lay.addWidget(QLabel("イン／アウトの動き。\n"
                             "Text+タイトルで出力すると、タイムラインで尺を変えても動きが追従します。"))
        tg = QGroupBox("対象")
        tl_ = QHBoxLayout(tg)
        self.rb_each = QRadioButton("テロップごと")
        self.rb_group = QRadioButton("まとめて（全テロップを一緒に動かす）")
        self.rb_each.setChecked(True)
        self.rb_group.toggled.connect(self._anim_target)
        tl_.addWidget(self.rb_each)
        tl_.addWidget(self.rb_group)
        lay.addWidget(tg)
        g = QGroupBox("イン（出てくるとき）")
        f = QFormLayout(g)
        c = QComboBox()
        for label, key in A.MODES:
            c.addItem(label, key)
        f.addRow("動き", self._bind(c, ("anim", "in"), "combo"))
        c = QComboBox()
        for label, key in A.EASES:
            c.addItem(label, key)
        f.addRow("緩急", self._bind(c, ("anim", "ease_in"), "combo"))
        lay.addWidget(g)
        g = QGroupBox("アウト（消えるとき）")
        f = QFormLayout(g)
        c = QComboBox()
        for label, key in A.MODES:
            c.addItem(label, key)
        f.addRow("動き", self._bind(c, ("anim", "out"), "combo"))
        c = QComboBox()
        for label, key in A.EASES:
            c.addItem(label, key)
        f.addRow("緩急", self._bind(c, ("anim", "ease_out"), "combo"))
        lay.addWidget(g)
        g = QGroupBox("共通")
        f = QFormLayout(g)
        f.addRow("長さ", self._bind(spin(0.05, 10, 0.05, 2, " 秒"), ("anim", "dur"), "float"))
        f.addRow("ズーム倍率", self._bind(spin(1.0, 20, 0.1, 2, " 倍"), ("anim", "zoom"), "float"))
        f.addRow("ぼかし量", self._bind(spin(0, 300, 1, 0, " px"), ("anim", "blur"), "float"))
        c = QComboBox()
        for label, key in A.PIVOT_MODES:
            c.addItem(label, key)
        f.addRow("拡大の中心", self._bind(c, ("anim", "pivot_mode"), "combo"))
        pv = QHBoxLayout()
        pv.addWidget(QLabel("X"))
        pv.addWidget(self._bind(spin(-10000, 10000, 1, 1), ("anim", "pivot", 0), "float"))
        pv.addWidget(QLabel("Y"))
        pv.addWidget(self._bind(spin(-10000, 10000, 1, 1), ("anim", "pivot", 1), "float"))
        f.addRow("", pv)
        b = QPushButton("プレビューをクリックして中心を指定")
        b.clicked.connect(self._pick_pivot)
        f.addRow("", b)
        f.addRow("", self._bind(QCheckBox("モーションブラー（重いので必要なときだけ）"), ("anim", "motion_blur"), "bool"))
        lay.addWidget(g)
        g = QGroupBox("アースクエイク（揺れ）")
        f = QFormLayout(g)
        f.addRow("揺れ幅", self._bind(spin(0, 500, 1, 0, " px"), ("anim", "quake_amp"), "float"))
        f.addRow("速さ", self._bind(spin(0.5, 60, 0.5, 1, " 回/秒"), ("anim", "quake_speed"), "float"))
        f.addRow("回転の揺れ", self._bind(spin(0, 45, 0.5, 1, " °"), ("anim", "quake_rot"), "float"))
        f.addRow("", self._bind(QCheckBox("ずっと揺らす（イン/アウトに関係なく最初から最後まで）"),
                                ("anim", "quake_hold"), "bool"))
        lbl = QLabel("イン/アウトの動きで「アースクエイク」を選ぶと、揺れながら出てきて止まる／揺れ始めて消えます。")
        lbl.setWordWrap(True)
        f.addRow(lbl)
        lay.addWidget(g)
        row = QHBoxLayout()
        self.btn_play = QPushButton("▶ プレビュー再生")
        self.btn_play.setCheckable(True)
        self.btn_play.toggled.connect(self._play_toggled)
        row.addWidget(self.btn_play)
        b = QPushButton("選択中のテロップ全部に同じ動き")
        b.clicked.connect(self._anim_to_selected)
        row.addWidget(b)
        lay.addLayout(row)
        lay.addWidget(QLabel("プレビュー再生は、下の「尺」の長さでくり返します。"))
        lay.addStretch(1)
        return w

    # ---------- アニメのプレビュー ----------
    def _anchor_ui(self):
        """アンカーの表示を今のテロップに合わせる"""
        ax, ay = R.anchor_of(self.t)
        old = self.loading
        self.loading = True
        try:
            hx, vy = ax * 2 - 1, ay * 2 - 1
            for axis, v in (("h", hx), ("v", vy)):
                sl, sp = self.anc_w[axis]
                sl.setValue(int(round(v * 100)))
                sp.setValue(v)
            for btn, bx, by in self.anc_cells:
                on = abs(hx - bx) < 1e-6 and abs(vy - by) < 1e-6
                btn.setText("●" if on else "・")
        finally:
            self.loading = old

    def _anchor_set_both(self, hx, vy):
        self._anchor_changed(((hx + 1) / 2, (vy + 1) / 2))

    def _anchor_set(self, axis, v):
        if self.loading:
            return
        v = max(-1.0, min(1.0, float(v)))
        ax, ay = R.anchor_of(self.t)
        if axis == "h":
            ax = (v + 1) / 2
        else:
            ay = (v + 1) / 2
        self._anchor_changed((round(ax, 4), round(ay, 4)))

    def _anchor_changed(self, key):
        """アンカーを変えても見た目の位置は動かさない。key=(ax, ay)"""
        o = self.t
        blk = R.block_rect(o)
        new_pos = R.anchor_point(o, key)
        # 回転・拡大している場合は、基準点が変わる分の見た目のずれを打ち消す
        from PySide6.QtGui import QTransform
        def tr_for(p):
            xf = o.get("xf") or {}
            t = QTransform()
            t.translate(*p)
            t.rotate(-float(xf.get("rot", 0)))
            t.scale(float(xf.get("sx", 100)) / 100, float(xf.get("sy", 100)) / 100)
            t.translate(-p[0], -p[1])
            return t
        old_c = tr_for(o["pos"]).map(blk.center())
        o["anchor"] = [key[0], key[1]]
        o["pos"] = new_pos
        new_c = tr_for(new_pos).map(R.block_rect(o).center())
        o["pos"][0] += old_c.x() - new_c.x()
        o["pos"][1] += old_c.y() - new_c.y()
        self.load_ui()
        self.schedule()

    def _xf_dragged(self, rot, sx, sy):
        self.t["xf"] = {"rot": round(rot, 2), "sx": round(sx, 2), "sy": round(sy, 2)}
        self.loading = True
        for w, path, kind in self.binds:
            if path and path[0] == "xf":
                w.setValue(self.t["xf"][path[1]])
        self.loading = False
        self.dirty = True
        self.do_render()

    def _keep_aspect(self, which, v):
        if self.loading or not self.chk_keep.isChecked():
            return
        other = self.sp_sy if which == "sx" else self.sp_sx
        if abs(other.value() - v) > 1e-6:
            other.setValue(v)

    def _reset_xf(self):
        self.t["xf"] = {"rot": 0.0, "sx": 100.0, "sy": 100.0}
        self.load_ui()
        self.schedule()

    # ---------- アニメ: 対象と中心 ----------
    def _anim_target(self):
        if self.loading:
            return
        self.doc.setdefault("ganim", copy.deepcopy(model.GROUP_ANIM))
        on = self.rb_group.isChecked()
        ga0 = self.doc["ganim"]
        if on and not ga0.get("enabled") and ga0.get("in", "none") == "none" and ga0.get("out", "none") == "none":
            ga = copy.deepcopy(self.t.get("anim") or {})          # まだ何も無ければ今のテロップの動きを初期値に
            ga.pop("pivot_mode", None)
            ga.pop("pivot", None)
            ga0.update(ga)
        self.doc["ganim"]["enabled"] = on
        self.load_ui()
        self.schedule()

    def _pick_pivot(self):
        self.preview.pick_pivot = True
        self.preview.setCursor(Qt.CrossCursor)
        self.statusBar().showMessage("プレビュー上で拡大の中心にしたい所をクリックしてください")

    def _pivot_picked(self, x, y):
        a = self._anim_dict()
        a["pivot_mode"] = "custom"
        a["pivot"] = [round(x, 1), round(y, 1)]
        self.load_ui()
        self.schedule()

    def _anim_dict(self):
        ga = self.doc.get("ganim") or {}
        if ga.get("enabled"):
            return ga
        self.t.setdefault("anim", {})
        return self.t["anim"]

    def _pivot_now(self):
        """プレビューに出す拡大の中心(px)。アニメがなければ None"""
        try:
            o = self.t
            ga = self.doc.get("ganim") or {}
            target = {"anim": ga} if ga.get("enabled") else o
            if not A.active(target):
                return None
            gc = TO.group_center(self.doc, self._obj_render) if ga.get("enabled") else None
            return A.effective(self.doc, o, gc)[1]
        except Exception:
            return None

    def _anim_to_selected(self):
        a = copy.deepcopy(self.t.get("anim") or {})
        for i in self.sel:
            self.doc["objects"][i]["anim"] = copy.deepcopy(a)
        self.schedule()
        self.statusBar().showMessage(f"{len(self.sel)} 個のテロップに同じ動きを設定しました")

    def _play_toggled(self, on):
        if not hasattr(self, "play_timer"):
            self.play_timer = QTimer(self, interval=33, timeout=self._play_tick)
        if on:
            self._play_f = 0
            self.btn_play.setText("■ 停止")
            self.play_timer.start()
        else:
            self.play_timer.stop()
            self.btn_play.setText("▶ プレビュー再生")
            self.do_render()

    def _obj_render(self, o):
        key = json.dumps(o, sort_keys=True, ensure_ascii=False)
        if key not in self.rcache:
            self.rcache[key] = R.render(o)
        return self.rcache[key]

    def _obj_image(self, o):
        return self._obj_render(o)[0]

    def _play_tick(self):
        fps = 29.97
        total = max(2, int(round(self.sp_dur.value() * fps)))
        f = self._play_f % (total + 15)             # 終わりで少し止めてからくり返す
        self._play_f += 1
        f = min(f, total - 1)
        self._render_frame(f, total, fps)

    def _tall(self, o, horiz=None):
        horiz = RL.horizontal(o) if horiz is None else horiz
        key = f"tall{int(horiz)}:" + json.dumps(o, sort_keys=True, ensure_ascii=False)
        if key not in self.rcache:
            self.rcache[key] = R.render_tall(o, horiz)
        return self.rcache[key]

    def _box_img(self, o):
        key = "box:" + json.dumps(o, sort_keys=True, ensure_ascii=False)
        if key not in self.rcache:
            self.rcache[key] = R.render_box(o)
        return self.rcache[key]

    def _roll_scrub(self, v):
        if self.loading or not RL.active(self.t):
            return
        fps = 29.97
        total = max(2, int(round(self.sp_dur.value() * fps)))
        self._render_frame(int(round(v / 1000.0 * (total - 1))), total, fps)

    def _roll_scrub_end(self):
        pass

    def _roll_info(self):
        """ロールの速さと、流れ切るのに必要な長さを表示"""
        if not hasattr(self, "lbl_roll"):
            return
        o = self.t
        if hasattr(self, "cmb_roll_end"):               # 「中央で止める」の言い方をロール／クロールで変える
            i = self.cmb_roll_end.findData("center")
            lab = RL.end_label(RL.get(o)["dir"], "center")
            if i >= 0 and self.cmb_roll_end.itemText(i) != lab:
                self.cmb_roll_end.setItemText(i, lab)
            loop = RL.looping(o)                        # ループは始まり・終わりを使わない
            self.cmb_roll_end.setEnabled(not loop)
            self.cmb_roll_start.setEnabled(not loop)
        if not RL.active(o):
            self.lbl_roll.setText("")
            return
        fps = 29.97
        total = max(2, int(round(self.sp_dur.value() * fps)))
        W, H = (int(v) for v in self.doc["canvas"])
        v, need = RL.speed(o, W, H, total)
        if RL.looping(o):
            msg = f"速さ {v:.1f} px／フレーム（約 {v * fps:.0f} px／秒）。1回分は約 {need / fps:.1f} 秒で、尺の終わりまでくり返します"
            self.lbl_roll.setText(msg)
            return
        msg = f"速さ {v:.1f} px／フレーム（約 {v * fps:.0f} px／秒）。流れ切るのに約 {need / fps:.1f} 秒"
        if need > total + 1:
            msg += f"\n⚠ 今の尺（{self.sp_dur.value():.1f} 秒）では最後まで流れません"
        self.lbl_roll.setText(msg)

    def _render_frame(self, f, total, fps):
        W, H = (int(v) for v in self.doc["canvas"])
        out = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
        out.fill(0)
        p = QPainter(out)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        gcen = TO.group_center(self.doc, self._obj_render) if (self.doc.get("ganim") or {}).get("enabled") else None
        for o in self.doc["objects"]:
            eo, (pcx, pcy) = A.effective(self.doc, o, gcen)
            tgt = TO.follow_target(self.doc, o) if R.is_image(o) else None
            if RL.active(o) or tgt is not None:       # ロール／クロール: 縦長（横長）の画像を動かして描く
                src = tgt if tgt is not None else o    # 画像は選んだテロップと同じ動きで流れる
                timg, top = self._tall(o, RL.horizontal(src))
                d = RL.d_at(src, f, total, W, H)
                xy = (int(round(top + d)), 0) if RL.horizontal(src) else (0, int(round(top + d)))
                bimg = self._box_img(o)                # 固定の座布団は流さない
                p.save()
                if A.active(eo):
                    r = A.compute(eo, f, total, fps)
                    p.setOpacity(max(0.0, min(1.0, r["op"])))
                    p.translate(r["dx"] * W + r.get("dxp", 0.0), r["dy"] * H + r.get("dyp", 0.0))
                if bimg is not None:
                    p.drawImage(0, 0, bimg)
                p.drawImage(xy[0], xy[1], timg)
                p.restore()
                continue
            img = self._obj_image(o)
            if not A.active(eo):
                p.drawImage(0, 0, img)
                continue
            r = A.compute(eo, f, total, fps)
            if r["op"] <= 0.001 or r["sx"] <= 0.001 or r["sy"] <= 0.001:
                continue
            if r["blur"] > 0.5:                     # 簡易ぼかし（縮小→拡大）
                k = 1.0 + r["blur"] / 6.0
                img = img.scaled(max(1, int(W / k)), max(1, int(H / k)), Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation).scaled(W, H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            cx, cy = pcx, pcy
            p.save()
            p.setOpacity(max(0.0, min(1.0, r["op"])))
            p.translate(cx + r["dx"] * W + r.get("dxp", 0.0), cy + r["dy"] * H + r.get("dyp", 0.0))
            if r.get("rot"):
                p.rotate(r["rot"])
            p.scale(r["sx"], r["sy"])
            p.translate(-cx, -cy)
            p.drawImage(0, 0, img)
            p.restore()
        p.end()
        free = [False] * len(self.doc["objects"])
        self.preview.set_image(out, self.last_infos, tuple(self.doc["canvas"]), free, set(), self.cur)

    def _style_strip(self):
        """プレビューの下のスタイル棚。クリックで即適用、＋で今の装飾をすぐ登録"""
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(6)
        col = QVBoxLayout()
        col.setSpacing(3)
        b = QPushButton("＋ 登録")
        b.setToolTip("選択中のテロップの装飾を、新しいスタイルとして登録します（名前はタイルをダブルクリックで変更）")
        b.setStyleSheet("QPushButton{background:#2f6fd6;color:white;font-weight:bold;padding:3px 8px;}")
        b.clicked.connect(self.style_save)
        col.addWidget(b)
        b = QPushButton("上書き")
        b.setToolTip("選んでいるタイルを、今の装飾で上書きします")
        b.clicked.connect(self.style_overwrite)
        col.addWidget(b)
        self.chk_keep_size = QCheckBox("サイズ保持")
        self.chk_keep_size.setToolTip("適用するとき文字サイズは今のまま。縁・影などはそのサイズに合わせて縮尺します")
        self.chk_keep_size.setChecked(bool(self.settings.get("style_keep_size", False)))
        self.chk_keep_size.toggled.connect(lambda v: self.settings.__setitem__("style_keep_size", bool(v)))
        col.addWidget(self.chk_keep_size)
        col.addStretch(1)
        lay.addLayout(col)
        self.lst_styles = QListWidget()
        self.lst_styles.setViewMode(QListView.IconMode)
        self.lst_styles.setFlow(QListView.LeftToRight)
        self.lst_styles.setWrapping(False)
        self.lst_styles.setIconSize(QSize(80, 80))
        self.lst_styles.setGridSize(QSize(96, 108))
        self.lst_styles.setMovement(QListView.Static)
        self.lst_styles.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.lst_styles.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.lst_styles.setMinimumHeight(118)
        self.lst_styles.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.lst_styles.setContextMenuPolicy(Qt.CustomContextMenu)
        self.lst_styles.customContextMenuRequested.connect(self._style_menu)
        self.lst_styles.setStyleSheet("QListWidget{background:#2a2a2a;color:#e8e8e8;border:none;}"
                                      "QListWidget::item:selected{background:#2f6fd6;color:white;}")
        self.lst_styles.itemClicked.connect(lambda it: self.style_apply(it))
        self.lst_styles.itemChanged.connect(self._style_renamed)
        self.lst_styles.setToolTip("クリック：選択中のテロップに適用　ダブルクリック：名前を変更　右クリック：上書き・削除など")
        lay.addWidget(self.lst_styles, 1)
        self._refresh_styles()
        return w

    def _style_menu(self, pos):
        it = self.lst_styles.itemAt(pos)
        m = QMenu(self)
        if it:
            m.addAction("適用", lambda: self.style_apply(it))
            m.addAction("今の装飾で上書き", lambda: self.style_overwrite(it))
            m.addAction("名前を変更", lambda: self.lst_styles.editItem(it))
            m.addAction("削除…", lambda: self.style_delete(it))
            m.addSeparator()
        m.addAction("今の装飾を登録", self.style_save)
        m.addAction("今の装飾を「新規」の初期値にする", self.style_set_default)
        m.addSeparator()
        m.addAction("見本の文字を変える…", self._sample_edit)
        m.exec(self.lst_styles.mapToGlobal(pos))

    def _sample_edit(self):
        txt, ok = QInputDialog.getText(self, "見本の文字", "タイルに表示する文字（1文字がおすすめ）:",
                                       text=self._sample_text())
        if ok:
            self.settings["sample"] = txt
            self.thumb_cache.clear()
            self._refresh_styles()

    def _style_renamed(self, it):
        old = it.data(Qt.UserRole)
        new = it.text().strip()
        if not old or new == old:
            return
        if not new or new in self.styles or new.startswith("__"):
            self.lst_styles.blockSignals(True)
            it.setText(old)
            self.lst_styles.blockSignals(False)
            self.statusBar().showMessage("その名前は使えません（空・同じ名前がある）")
            return
        self.styles[new] = self.styles.pop(old)
        it.setData(Qt.UserRole, new)
        model.save_json(STYLES_PATH, self.styles)

    # ---------- バインド ----------
    def _bind(self, w, path, kind):
        self.binds.append((w, path, kind))
        cb = (lambda *_a, w=w, p=path, k=kind: self._changed(w, p, k))
        if kind in ("int", "float", "pct"):
            w.valueChanged.connect(cb)
        elif kind == "bool":
            w.toggled.connect(cb)
        elif kind == "color":
            w.changed.connect(cb)
        elif kind == "combo":
            w.currentIndexChanged.connect(cb)
        return w

    def _root_for(self, path):
        if path and path[0] == "anim" and (self.doc.get("ganim") or {}).get("enabled"):
            for k, v in A.DEFAULT.items():
                self.doc["ganim"].setdefault(k, copy.deepcopy(v))
            return {"anim": self.doc["ganim"]}
        if path and path[0] == "anim":
            a = self.t.setdefault("anim", {})
            for k, v in A.DEFAULT.items():
                a.setdefault(k, copy.deepcopy(v))
        return self.t

    def _get(self, path):
        d = self._root_for(path)
        for k in path:
            d = d[k]
        return d

    def _set(self, path, v):
        d = self._root_for(path)
        for k in path[:-1]:
            d = d[k]
        d[path[-1]] = v

    def _changed(self, w, path, kind):
        if self.loading:
            return
        if kind == "int":
            v = int(w.value())
        elif kind == "float":
            v = float(w.value())
        elif kind == "pct":
            v = w.value() / 100.0
        elif kind == "bool":
            v = bool(w.isChecked())
        elif kind == "combo":
            v = w.currentData()
        else:
            v = w.color()
        if self.char_sel:                       # 文字を選んでいるときは、その文字だけに
            if path == ("font", "size"):
                base = float(self.t["font"]["size"]) or 1.0
                self._cs_apply("size", float(v) / base)
                return
            if path == ("face", "color"):
                self._cs_apply("face", v)
                return
            if len(path) == 3 and path[0] == "edges" and path[2] == "color":
                self._cs_apply("edge", v, path[1])
                return
            if len(path) == 3 and path[0] == "edges" and path[2] in ("width", "enabled"):
                i = path[1]
                if path[2] == "width":
                    w_ = float(v)
                else:
                    gw = float(self.t["edges"][i].get("width", 0) or 0)
                    w_ = (gw if gw > 0 else 4.0) if v else 0.0
                self._cs_apply("merge", {"ew": {str(i): round(w_, 3)}})
                return
            if len(path) == 3 and path[0] == "edges" and path[2] in ("soft", "opacity"):
                key = "es" if path[2] == "soft" else "eo"
                self._cs_apply("merge", {key: {str(path[1]): round(float(v), 4)}})
                return
            sh_keys = {"enabled": "sh_on", "color": "sh_color", "blur": "sh_blur", "opacity": "sh_op",
                       "distance": "sh_dist", "angle": "sh_angle"}
            if len(path) == 2 and path[0] == "shadow" and path[1] in sh_keys:
                self._cs_apply("merge", {sh_keys[path[1]]: v})
                return
        if path == ("font", "size"):
            old = float(self._get(path) or v)
            if old > 0 and v != old:
                if self.chk_scale_deco.isChecked():
                    model.scale_deco(self.t, float(v) / old)    # 装飾も同じ比率で（文字ごとの指定も）
                else:
                    model.scale_runs(self.t, float(v) / old)    # カーニング・ベースラインは常に文字の大きさに合わせる
                self._set(path, v)
                self.load_ui()                                # 縁などの数値表示を更新
                self.schedule()
                return
        self._set(path, v)
        if R.is_image(self.t) and path == ("xf", "sx"):     # 画像の大きさは縦横比を保つ
            self.t["xf"]["sy"] = v
        self.schedule()

    def load_ui(self):
        self.loading = True
        try:
            for w, path, kind in self.binds:
                v = self._get(path)
                if kind == "pct":
                    w.setValue(round(float(v) * 100))
                elif kind in ("int", "float"):
                    w.setValue(v)
                elif kind == "bool":
                    w.setChecked(bool(v))
                elif kind == "color":
                    w.setColor(v)
                elif kind == "combo":
                    w.setCurrentIndex(max(0, w.findData(v)))
            if (self.txt.toPlainText() != self.t["text"]
                    or self._runs_from_doc() != model.compress_runs(R.char_styles(self.t))):
                pos = self.txt.textCursor().position()
                self.txt.setPlainText(self.t["text"])
                self._runs_to_doc(self.t.get("runs") or [])
                c = self.txt.textCursor()
                c.setPosition(min(pos, len(self.t["text"])))
                self.txt.setTextCursor(c)
            self.cmb_font.setCurrentFont(QFont(self.t["font"]["family"]))
            self._fill_styles_combo(self.t["font"]["family"], self.t["font"].get("style", ""))
            for b in self.align_group.buttons():
                b.setChecked(b.property("key") == self.t["font"].get("align", "center"))
            self._anchor_ui()
            g_on = bool((self.doc.get("ganim") or {}).get("enabled"))
            self.rb_group.setChecked(g_on)
            self.rb_each.setChecked(not g_on)
            self.grad.setStops(self.t["face"].get("stops"))
            fi = self.t["face"].get("img") or {}
            self.lbl_face_img.setText(fi.get("name") or "（未選択）"
                                      if not fi.get("file") or os.path.isfile(fi.get("file", "")) else
                                      f"{fi.get('name')}（ファイルが見つかりません）")
            for i_, ged in getattr(self, "edge_grads", {}).items():
                ged.setStops(self.t["edges"][i_].get("stops"))
            self._grad_sel_changed()
            self.cmb_span.setCurrentIndex(0 if self.t["face"].get("span", "line") == "line" else 1)
            free = self.t["box"].get("mode") == "free"
            self.rb_free.setChecked(free)
            self.rb_fit.setChecked(not free)
            self.box_fit_w.setVisible(not free)
            self.box_free_w.setVisible(free)
            i = self.cmb_mode.findData(self.doc.get("mode", "single"))
            self.cmb_mode.setCurrentIndex(max(0, i))
            if self.editing:
                j = self.cmb_out.findData(self.editing.get("output", "png"))
                self.cmb_out.setCurrentIndex(max(0, j))
            self._refresh_obj_list()
            for sp_ in (self.sp_kern, self.sp_bs):
                sp_.setValue(0)
                sp_.setEnabled(False)
            self._image_ui()
        finally:
            self.loading = False
        if self.char_sel:
            self._char_show()

    def _fill_styles_combo(self, family, want):
        self.cmb_style.blockSignals(True)
        self.cmb_style.clear()
        styles = QFontDatabase.styles(family) or ["Regular"]
        self.cmb_style.addItems(styles)
        if want in styles:
            self.cmb_style.setCurrentText(want)
        self.cmb_style.blockSignals(False)
        return self.cmb_style.currentText()

    # ---------- 文字ごとの指定 ----------
    CS_PROP = 0x100000 + 7          # QTextFormat.UserProperty + 7（文字ごとの指定を JSON で持たせる）

    def _runs_from_doc(self):
        """文字入力欄の書式 → runs"""
        doc = self.txt.document()
        text = self.txt.toPlainText()
        cur = QTextCursor(doc)
        styles = []
        for i, ch in enumerate(text):
            if ch == "\n":
                styles.append({})
                continue
            cur.setPosition(i + 1)
            v = cur.charFormat().property(self.CS_PROP)
            try:
                styles.append(json.loads(v) if v else {})
            except Exception:
                styles.append({})
        return model.compress_runs(styles)

    def _cs_format(self, attrs):
        f = QTextCharFormat()
        f.setProperty(self.CS_PROP, json.dumps(attrs, sort_keys=True) if attrs else "")
        if attrs:
            f.setBackground(QColor(255, 200, 80, 60))
            f.setForeground(QColor(attrs["face"]) if attrs.get("face") else self.txt.palette().text().color())
            f.setFontUnderline(bool(attrs.get("size")) and abs(float(attrs["size"]) - 1) > 1e-6)
            if attrs.get("font"):
                f.setFontFamilies([attrs["font"][0]])
            else:
                f.setFontFamilies([self.txt.font().family()])
        else:
            f.setBackground(Qt.transparent)
            f.setForeground(self.txt.palette().text().color())
            f.setFontUnderline(False)
            f.setFontFamilies([self.txt.font().family()])
        return f

    def _runs_to_doc(self, runs):
        """runs → 文字入力欄の書式（見た目: 指定のある文字は薄い色の下地、面の色は文字色に）"""
        old = self.loading
        self.loading = True
        try:
            cur = QTextCursor(self.txt.document())
            cur.select(QTextCursor.Document)
            cur.setCharFormat(self._cs_format({}))
            n = len(self.txt.toPlainText())
            for r in runs or []:
                a = {k: v for k, v in r.items() if k not in ("s", "e")}
                s0, e0 = max(0, int(r["s"])), min(n, int(r["e"]))
                if e0 > s0:
                    cur.setPosition(s0)
                    cur.setPosition(e0, QTextCursor.KeepAnchor)
                    cur.setCharFormat(self._cs_format(a))
        finally:
            self.loading = old

    def _cs_range(self):
        return self.char_sel

    # 文字の選択（プレビュー / 文字入力欄）
    def _preview_char_sel(self, a, b):
        if a < 0:
            self._set_char_sel(None, sync_txt=True)
            # 打ち込まずに終えた空のテロップは片付ける（ほかにテロップがあるとき）
            if not model.has_content(self.t) and len(self.doc["objects"]) > 1:
                del self.doc["objects"][self.cur]
                self.cur = max(0, self.cur - 1)
                self.sel = {self.cur}
                self.load_ui()
                self.schedule()
        else:
            self._set_char_sel((a, b), sync_txt=True)

    def _txt_sel_changed(self):
        if self._csync or self.loading:
            return
        c = self.txt.textCursor()
        if self.txt.hasFocus() and self.preview.char_mode:
            self.preview.exit_char_mode(emit=False)      # 文字入力欄で作業を始めたら、プレビューの編集は終わり
        if c.hasSelection():
            self._set_char_sel((c.selectionStart(), c.selectionEnd()), sync_txt=False)
        elif self.char_sel and not self.preview.char_mode:
            self._set_char_sel(None, sync_txt=False)

    def _char_done(self):
        self.preview.exit_char_mode(emit=False)
        self._set_char_sel(None, sync_txt=True)

    def _set_char_sel(self, sel, sync_txt=True):
        n = len(self.t.get("text") or "")
        if sel is not None:
            sel = (max(0, min(n, sel[0])), max(0, min(n, sel[1])))
        real = sel is not None and sel[1] > sel[0]
        was = bool(self.char_sel)
        self.char_sel = sel if real else None
        self.char_uid = self.t.get("uid")
        self.preview.char_sel = sel
        if not self.preview.char_mode and not real:
            self.preview.char_sel = None
        self.preview.update()
        if sync_txt:
            self._csync = True
            try:
                c = self.txt.textCursor()
                if sel is not None:
                    c.setPosition(sel[0])
                    c.setPosition(sel[1], QTextCursor.KeepAnchor)
                else:
                    c.clearSelection()
                self.txt.setTextCursor(c)
            finally:
                self._csync = False
        if real:
            frag = self.t["text"][sel[0]:sel[1]].replace("\n", "⏎")
            if len(frag) > 12:
                frag = frag[:11] + "…"
            self.lbl_char.setText(f"「{frag}」だけを編集中\n"
                                  "フォント・サイズ・面の色・縁・影・スタイル棚が この文字だけに効きます")
            self.char_banner.show()
            self._char_show()
        else:
            self.char_banner.hide()
            if was:
                self.load_ui()

    def _char_show(self):
        """選んだ文字の今の指定を、フォント欄・サイズ・色ボタンに表示する"""
        if not self.char_sel:
            return
        st = R.char_styles(self.t)
        s0 = self.char_sel[0]
        while s0 < self.char_sel[1] - 1 and s0 < len(st) and self.t["text"][s0] == "\n":
            s0 += 1
        a = st[s0] if s0 < len(st) else {}
        old = self.loading
        self.loading = True
        try:
            fs = a.get("font") or [self.t["font"]["family"], self.t["font"].get("style", "")]
            self.cmb_font.setCurrentFont(QFont(fs[0]))
            self._fill_styles_combo(fs[0], fs[1] if len(fs) > 1 else "")
            self.sp_size.setValue(int(round(float(self.t["font"]["size"]) * float(a.get("size", 1)))))
            self.sp_kern.setEnabled(True)
            self.sp_bs.setEnabled(True)
            self.sp_kern.setValue(float(a.get("kern", 0) or 0))
            self.sp_bs.setValue(float(a.get("bs", 0) or 0))
            for w, path, kind in self.binds:
                if path == ("face", "color"):
                    w.setColor(a.get("face") or self.t["face"]["color"])
                elif len(path) == 3 and path[0] == "edges" and path[2] == "color":
                    w.setColor((a.get("edges") or {}).get(str(path[1])) or self.t["edges"][path[1]]["color"])
                elif len(path) == 3 and path[0] == "edges" and path[2] in ("width", "enabled"):
                    i = path[1]
                    e = self.t["edges"][i]
                    ew = (a.get("ew") or {}).get(str(i))
                    width = float(ew) if ew is not None else (float(e.get("width", 0)) if e.get("enabled") else 0.0)
                    if path[2] == "width":
                        w.setValue(width if width > 0 else float(e.get("width", 0)))
                    else:
                        w.setChecked(width > 0)
                elif len(path) == 3 and path[0] == "edges" and path[2] in ("soft", "opacity"):
                    e = self.t["edges"][path[1]]
                    key = "es" if path[2] == "soft" else "eo"
                    v = (a.get(key) or {}).get(str(path[1]))
                    v = float(v) if v is not None else float(e.get(path[2], 0 if key == "es" else 1))
                    w.setValue(v if key == "es" else round(v * 100))
                elif len(path) == 2 and path[0] == "shadow" and path[1] in ("enabled", "color", "blur", "opacity",
                                                                             "distance", "angle"):
                    on, col, blur, op, dist, ang = R.seg_shadow(self.t, a)
                    if path[1] == "enabled":
                        w.setChecked(bool(a.get("sh_on", self.t["shadow"].get("enabled"))))
                    elif path[1] == "color":
                        w.setColor(col)
                    elif path[1] == "blur":
                        w.setValue(blur)
                    elif path[1] == "distance":
                        w.setValue(dist)
                    elif path[1] == "angle":
                        w.setValue(int(round(ang)))
                    else:
                        w.setValue(round(op * 100))
        finally:
            self.loading = old

    # ---------- プレビューでの文字の打ち込み・コピー＆ペースト ----------
    MIME_STYLED = "application/x-ct-titler-styled-text"

    def _edit_range(self):
        sel = self.preview.char_sel or (len(self.t.get("text") or ""),) * 2
        return min(sel), max(sel)

    def _insert_at(self, s, e, text, styles=None):
        """文字入力欄の s〜e を text で置き換える（styles があれば文字ごとの指定も付ける）。-> 新しいカーソル位置"""
        doc = self.txt.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        cur.setPosition(s)
        cur.setPosition(e, QTextCursor.KeepAnchor)
        if styles is None:
            # 打った文字は、前の文字（先頭なら次の文字）の指定を引き継ぐ
            st = R.char_styles(self.t)
            ref = s - 1 if s > 0 else s
            a = st[ref] if 0 <= ref < len(st) and self.t["text"][ref] != "\n" else {}
            cur.insertText(text, self._cs_format(a))
        else:
            cur.removeSelectedText()
            for ch, a in zip(text, styles):
                cur.insertText(ch, self._cs_format({} if ch == "\n" else (a or {})))
        cur.endEditBlock()
        return s + len(text)

    def _edit_insert(self, text):
        if not text:
            return
        s, e = self._edit_range()
        k = self._insert_at(s, e, text.replace("\r\n", "\n").replace("\r", "\n"))
        self._edit_caret(k)

    def _edit_caret(self, k):
        self.preview.char_mode = True
        self.preview.char_sel = (k, k)
        self._set_char_sel(None, sync_txt=False)
        self.preview.char_sel = (k, k)
        self.preview.setFocus()
        self.preview.update()

    def _clip_styled(self, s, e):
        text = (self.t.get("text") or "")[s:e]
        st = R.char_styles(self.t)[s:e]
        return text, st

    def _edit_copy(self, s, e):
        if e <= s:
            return False
        from PySide6.QtCore import QMimeData
        text, st = self._clip_styled(s, e)
        md = QMimeData()
        md.setText(text)
        md.setData(self.MIME_STYLED, json.dumps({"text": text, "styles": st}, ensure_ascii=False).encode("utf-8"))
        QApplication.clipboard().setMimeData(md)
        return True

    def _clip_get(self):
        md = QApplication.clipboard().mimeData()
        if md is None:
            return None, None
        if md.hasFormat(self.MIME_STYLED):
            try:
                d = json.loads(bytes(md.data(self.MIME_STYLED)).decode("utf-8"))
                return d["text"], d["styles"]
            except Exception:
                pass
        if md.hasText():
            return md.text().replace("\r\n", "\n").replace("\r", "\n"), None
        return None, None

    def _txt_paste_styled(self, md):
        """文字入力欄への貼り付け（CT Titler からのコピーなら文字ごとの指定も）"""
        try:
            d = json.loads(bytes(md.data(self.MIME_STYLED)).decode("utf-8"))
        except Exception:
            return False
        c = self.txt.textCursor()
        s, e = c.selectionStart(), c.selectionEnd()
        k = self._insert_at(s, e, d["text"], d.get("styles"))
        c = self.txt.textCursor()
        c.setPosition(k)
        self.txt.setTextCursor(c)
        return True

    def _edit_command(self, cmd):
        s, e = self._edit_range()
        n = len(self.t.get("text") or "")
        if cmd in ("copy", "cut"):
            if self._edit_copy(s, e):
                self.statusBar().showMessage(f"{e - s} 文字をコピーしました（文字ごとの指定も一緒に）")
                if cmd == "cut":
                    self._edit_caret(self._insert_at(s, e, ""))
            return
        if cmd == "paste":
            text, st = self._clip_get()
            if text:
                k = self._insert_at(s, e, text, st)
                self._edit_caret(k)
            return
        if cmd == "backspace":
            if e > s:
                self._edit_caret(self._insert_at(s, e, ""))
            elif s > 0:
                self._edit_caret(self._insert_at(s - 1, s, ""))
            return
        if cmd == "delete":
            if e > s:
                self._edit_caret(self._insert_at(s, e, ""))
            elif s < n:
                self._edit_caret(self._insert_at(s, s + 1, ""))
            return

    def _char_nudge(self, dk, db):
        """⌥矢印: 選んだ文字（カーソルだけならその前の1文字）のカーニング・ベースラインを増減"""
        sel = self.preview.char_sel
        if not sel:
            return
        rng = sel if sel[1] > sel[0] else ((sel[0] - 1, sel[0]) if sel[0] > 0 else None)
        if not rng:
            return
        add = {}
        if dk:
            add["kern"] = dk
        if db:
            add["bs"] = db
        self._cs_apply("add", add, rng=rng)
        if rng != sel:                      # カーソルだけだったときは、調整した文字を選んだ状態に
            self.preview.char_sel = rng
            self._set_char_sel(rng, sync_txt=True)

    def _cs_apply(self, key, value, edge=None, rng=None):
        if self.loading:
            return
        rng = rng or self._cs_range()
        if not rng or rng[1] <= rng[0]:
            self.statusBar().showMessage("文字入力欄で、変えたい文字を選んでください")
            return
        st = R.char_styles(self.t)
        text = self.t["text"]
        for i in range(rng[0], min(rng[1], len(st))):
            if text[i] == "\n":                    # 改行には指定を持たせない
                continue
            a = dict(st[i])
            if key == "clear":
                a = {}
            elif key == "size":
                if abs(value - 1) < 1e-6:
                    a.pop("size", None)
                else:
                    a["size"] = round(float(value), 4)
            elif key == "face":
                a["face"] = value
            elif key == "add":
                for kk, vv in value.items():
                    nv = round(float(a.get(kk, 0) or 0) + float(vv), 2)
                    if abs(nv) < 1e-6:
                        a.pop(kk, None)
                    else:
                        a[kk] = nv
            elif key == "merge":
                for kk, vv in value.items():
                    if kk in ("edges", "ew"):
                        ed = dict(a.get(kk) or {})
                        ed.update(vv)
                        a[kk] = ed
                    elif vv is None:
                        a.pop(kk, None)
                    else:
                        a[kk] = vv
            elif key == "font":
                main = [self.t["font"]["family"], self.t["font"].get("style", "")]
                if list(value) == main:
                    a.pop("font", None)
                else:
                    a["font"] = list(value)
            elif key == "edge":
                ed = dict(a.get("edges") or {})
                ed[str(edge)] = value
                a["edges"] = ed
            st[i] = a
        self.t["runs"] = model.compress_runs(st)
        self._runs_to_doc(self.t["runs"])
        self._csync = True
        try:
            c = self.txt.textCursor()                   # 選択を保ったまま
            c.setPosition(rng[0])
            c.setPosition(rng[1], QTextCursor.KeepAnchor)
            self.txt.setTextCursor(c)
        finally:
            self._csync = False
        self._char_show()
        self.schedule()

    def _text_changed(self):
        if self.loading:
            return
        self.t["text"] = self.txt.toPlainText()
        self.t["runs"] = self._runs_from_doc()
        it = self.lst_objs.item(len(self.doc["objects"]) - 1 - self.cur)
        if it:
            it.setText(obj_label(self.t))
        self.schedule()

    def _face_img_pick(self):
        r = self._pick_image()
        if not r:
            return
        dst, w, h, name = r
        self.t["face"]["img"].update({"file": dst, "w": int(w), "h": int(h), "name": name})
        self.t["face"]["img_on"] = True
        self.load_ui()
        self.schedule()

    def _edge_grad_changed(self, i):
        if self.loading:
            return
        self.t["edges"][i]["stops"] = self.edge_grads[i].stops()
        self.schedule()

    def _grad_changed(self):
        if self.loading:
            return
        self.t["face"]["stops"] = self.grad.stops()
        st = self.t["face"]["stops"]
        self.t["face"]["color2"] = st[-1][1]
        self.loading = True
        self._grad_sel_changed()
        self.loading = False
        self.schedule()

    def _grad_sel_changed(self):
        s = self.grad.selected()
        if s is None:
            return
        old = self.loading
        self.loading = True
        self.grad_color.setColor(s[1])
        self.grad_pos.setValue(s[0] * 100)
        self.grad_alpha.setValue(round(s[2] * 100))
        self.loading = old

    def _grad_angle(self, a):
        self.t["face"]["angle"] = a
        self.load_ui()
        self.schedule()

    def _grad_span(self):
        if self.loading:
            return
        self.t["face"]["span"] = self.cmb_span.currentData()
        self.schedule()

    def _grad_preset(self):
        name = self.cmb_gpreset.currentData()
        if not name:
            return
        self.t["face"]["stops"] = copy.deepcopy(model.GRADIENT_PRESETS[name])
        self.t["face"]["gradient"] = True
        self.cmb_gpreset.setCurrentIndex(0)
        self.load_ui()
        self.schedule()

    def _font_sample(self):
        """フォント一覧の見本の文字（今のテロップの1行目。なければ定番の見本）"""
        try:
            line = (self.t.get("text") or "").strip().split("\n")[0].strip()
        except Exception:
            line = ""
        return line[:14] if line else "テロップ あア亜 Abc"

    def _font_hover(self, idx):
        """一覧でカーソルを合わせたフォントを、プレビューに仮で当てる"""
        fam = self.cmb_font.itemText(idx)
        if self.char_sel:
            return          # 文字を選んでいるときは仮当てしない
        if not fam or self.loading or not self.cmb_font.view().isVisible():
            return          # 一覧を開いているときだけ（起動時や読み込み時の通知は無視）
        if self._font_orig is None:
            self._font_orig = (self.t["font"]["family"], self.t["font"].get("style", ""))
        self.t["font"]["family"] = fam
        styles = QFontDatabase.styles(fam)
        want = self._font_orig[1]
        self.t["font"]["style"] = want if want in styles else (styles[0] if styles else want)
        self.schedule()

    def _font_hover_end(self):
        """一覧を閉じたら、選んだフォント（選ばなければ元のフォント）に戻す"""
        def fin():
            if self._font_orig is None:
                return
            fam0, st0 = self._font_orig
            self._font_orig = None
            chosen = self.cmb_font.currentFont().family()
            if chosen == fam0:
                self.t["font"]["family"], self.t["font"]["style"] = fam0, st0
            else:
                self.t["font"]["family"] = chosen
                self.t["font"]["style"] = self._fill_styles_combo(chosen, st0)
            self.schedule()
        QTimer.singleShot(0, fin)

    def _family_changed(self, font):
        if self.loading:
            return
        fam = font.family()
        if self.char_sel:
            st = self._fill_styles_combo(fam, self.cmb_style.currentText() or self.t["font"].get("style", ""))
            self._cs_apply("font", [fam, st])
            return
        self.t["font"]["family"] = fam
        self.t["font"]["style"] = self._fill_styles_combo(fam, self.t["font"].get("style", ""))
        self.schedule()

    def _style_changed(self, s):
        if self.loading or not s:
            return
        if self.char_sel:
            self._cs_apply("font", [self.cmb_font.currentFont().family(), s])
            return
        self.t["font"]["style"] = s
        self.schedule()

    def _align_changed(self, b):
        self.t["font"]["align"] = b.property("key")
        self.schedule()

    def _needs_convert(self):
        ed = self.editing
        return bool(ed) and (self.cmb_out.currentData() != ed.get("output", "png")
                             or self.cmb_mode.currentData() != ed.get("mode", "single"))

    def _out_changed(self):
        out = self.cmb_out.currentData()
        labels = MODES_TP if out == "textplus" else MODES
        for i, (label, _) in enumerate(labels):
            self.cmb_mode.setItemText(i, label)
        if self._needs_convert():
            self.btn_place.setText(keys("この形式で置き直す (⌘↩)"))
        elif self.editing:
            self.btn_place.setText(keys("更新して差し替え (⌘↩)"))
        if self.loading:
            return
        if not self.editing:
            self.settings["output"] = out

    def _mode_changed(self):
        if self.loading:
            return
        self.doc["mode"] = self.cmb_mode.currentData()
        if not self.editing:
            self.settings["mode"] = self.doc["mode"]
        self._out_changed()

    def _set_pos(self, fx, fy):
        W, H = self.doc["canvas"]
        if fx is not None:
            self.t["pos"][0] = W * fx
        if fy is not None:
            self.t["pos"][1] = H * fy
        self._sync_pos()
        self.schedule()

    def _sync_pos(self):
        self.loading = True
        self.sp_x.setValue(self.t["pos"][0])
        self.sp_y.setValue(self.t["pos"][1])
        self.sp_bx.setValue(self.t["box"]["rect"][0])
        self.sp_by.setValue(self.t["box"]["rect"][1])
        self.loading = False

    def _bg_mode(self):
        self.preview.bg_mode = self.cmb_bg.currentData()
        self.preview.update()

    def _apply_on_top(self, v):
        self.settings["on_top"] = bool(v)
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(v))
        if visible:
            self.show()

    # ---------- テロップ一覧 ----------
    def _refresh_obj_list(self):
        """一覧は上に重なるテロップほど上に表示"""
        self.lst_objs.blockSignals(True)
        self.lst_objs.clear()
        n = len(self.doc["objects"])
        for i in range(n - 1, -1, -1):
            self.lst_objs.addItem(obj_label(self.doc["objects"][i]))
        self.lst_objs.setCurrentRow(n - 1 - self.cur)
        for i in self.sel:
            it = self.lst_objs.item(n - 1 - i)
            if it:
                it.setSelected(True)
        self.lst_objs.blockSignals(False)

    def _list_sel_changed(self):
        n = len(self.doc["objects"])
        rows = [self.lst_objs.row(it) for it in self.lst_objs.selectedItems()]
        if not rows:
            return
        self.sel = {n - 1 - r for r in rows}
        cr = self.lst_objs.currentRow()
        cur = n - 1 - cr if cr >= 0 else min(self.sel)
        if cur not in self.sel:
            cur = min(self.sel)
        if cur != self.cur:
            self.cur = cur
            self.load_ui()
        self._show_preview()

    def _picked(self, idx, additive):
        if additive:
            if idx in self.sel and len(self.sel) > 1:
                self.sel.discard(idx)
                if self.cur == idx:
                    self.cur = min(self.sel)
            else:
                self.sel.add(idx)
                self.cur = idx
        else:
            self.sel = {idx}
            self.cur = idx
        self.load_ui()
        self._show_preview()

    def obj_add(self):
        src = self.t
        o = model.apply_style(model.new_title(*self.doc["canvas"]), model.extract_style(src))
        o["text"] = "テロップ"
        H = self.doc["canvas"][1]
        step = src["font"]["size"] * 1.6
        y = src["pos"][1] + step if src["pos"][1] + step < H * 0.92 else src["pos"][1] - step
        o["pos"] = [src["pos"][0], y]
        o["box"]["mode"] = "fit"
        self.doc["objects"].insert(self.cur + 1, o)
        self.cur += 1
        self.sel = {self.cur}
        self.load_ui()
        self.schedule()
        self.txt.setFocus()
        self.txt.selectAll()

    def _new_text_at(self, x, y):
        """プレビューの何もない所をダブルクリック: 今の装飾で空のテロップを作り、その場で打ち込めるようにする"""
        src = self.t
        o = model.apply_style(model.new_title(*self.doc["canvas"]), model.extract_style(src))
        o["text"], o["runs"] = "", []
        o["anchor"] = list(R.anchor_of(src))
        o["pos"] = [x, y]
        o["box"]["mode"] = "fit"
        self.doc["objects"].insert(self.cur + 1, o)
        self.cur += 1
        self.sel = {self.cur}
        self.load_ui()
        self.do_render()
        self.preview.char_mode = True
        self.preview.char_sel = (0, 0)
        self.preview.setCursor(Qt.IBeamCursor)
        self.preview.setFocus()
        self.preview.update()
        self.statusBar().showMessage("新しいテロップ：そのまま打ち込めます（Esc で終了）")

    def obj_dup(self):
        o = copy.deepcopy(self.t)
        o["uid"] = model.new_uid()
        o["pos"] = [o["pos"][0] + 30, o["pos"][1] + 30]
        if o["box"].get("mode") == "free":
            o["box"]["rect"][0] += 30
            o["box"]["rect"][1] += 30
        self.doc["objects"].insert(self.cur + 1, o)
        self.cur += 1
        self.sel = {self.cur}
        self.load_ui()
        self.schedule()

    def obj_del(self):
        objs = self.doc["objects"]
        if len(self.sel) >= len(objs):
            # 全部消すときは、装飾はそのままの空のテロップを1つ残す（画面には何も出ない）
            keep = copy.deepcopy(self.t)
            keep["text"], keep["runs"], keep["kind"] = "", [], "text"
            keep["uid"] = model.new_uid()
            self.doc["objects"] = objs = [keep]
            self.cur, self.sel = 0, {0}
            self.load_ui()
            self.schedule()
            self.statusBar().showMessage("テロップを全部消しました")
            return
        for i in sorted(self.sel, reverse=True):
            if len(objs) > 1:
                del objs[i]
        self.cur = min(self.cur, len(objs) - 1)
        self.sel = {self.cur}
        self.load_ui()
        self.schedule()

    def obj_move(self, d):
        objs = self.doc["objects"]
        j = self.cur + d
        if 0 <= j < len(objs):
            objs[self.cur], objs[j] = objs[j], objs[self.cur]
            self.cur = j
            self.sel = {j}
            self.load_ui()
            self.schedule()

    # ---------- 移動・整列 ----------
    def _shift(self, o, dx, dy, part="all"):
        if part in ("text", "all"):
            o["pos"][0] += dx
            o["pos"][1] += dy
        if part in ("box", "all") and o["box"].get("mode") == "free":
            o["box"]["rect"][0] += dx
            o["box"]["rect"][1] += dy

    def _moved(self, target, dx, dy):
        if target == "all":
            for i in self.sel:
                self._shift(self.doc["objects"][i], dx, dy, "all")
        else:
            self._shift(self.t, dx, dy, target)
        self._sync_pos()
        self.dirty = True
        self.do_render()

    def _split_images(self, target):
        objs = self.doc["objects"]
        if target == "all":
            moving = set(self.sel)
            return (R.render_doc(self.doc, self.rcache, skip=moving)[0],
                    R.render_doc(self.doc, self.rcache, only=moving)[0])
        others = R.render_doc(self.doc, self.rcache, skip={self.cur})[0]
        o = objs[self.cur]
        free = o["box"].get("enabled") and o["box"].get("mode") == "free"
        if not free:
            return others, R.render(o)[0]
        moving_part = ("box",) if target == "box" else ("text",)
        still_part = ("text",) if target == "box" else ("box",)
        p = QPainter(others)
        p.drawImage(0, 0, R.render(o, still_part)[0])
        p.end()
        return others, R.render(o, moving_part)[0]

    def _obj_rect(self, i):
        info = self.last_infos[i] if i < len(self.last_infos) else None
        if info is None:
            return QRectF()
        r = QRectF(info["block"])
        if info.get("box") is not None:
            r = r.united(info["box"])
        return r

    def align(self, how):
        self.do_render()
        idx = sorted(self.sel)
        if not idx:
            return
        W, H = self.doc["canvas"]
        rects = {i: self._obj_rect(i) for i in idx}
        ref = self.cmb_ref.currentData()
        if ref == "sel" and len(idx) > 1:
            base = None
            for r in rects.values():
                base = QRectF(r) if base is None else base.united(r)
        elif ref == "safe":
            base = QRectF(W * 0.05, H * 0.05, W * 0.9, H * 0.9)
        else:
            base = QRectF(0, 0, W, H)
        objs = self.doc["objects"]
        gap = self.sp_gap.value()
        self.settings["gap"] = gap
        if how in ("left", "hcenter", "right", "top", "vcenter", "bottom"):
            for i in idx:
                r = rects[i]
                dx = dy = 0
                if how == "left":
                    dx = base.left() - r.left()
                elif how == "right":
                    dx = base.right() - r.right()
                elif how == "hcenter":
                    dx = base.center().x() - r.center().x()
                elif how == "top":
                    dy = base.top() - r.top()
                elif how == "bottom":
                    dy = base.bottom() - r.bottom()
                elif how == "vcenter":
                    dy = base.center().y() - r.center().y()
                self._shift(objs[i], dx, dy)
        elif how in ("hdist", "vdist"):
            if len(idx) < 3:
                self.statusBar().showMessage("等間隔は3つ以上選択してください")
                return
            horiz = how == "hdist"
            order = sorted(idx, key=lambda i: rects[i].center().x() if horiz else rects[i].center().y())
            first, last = rects[order[0]], rects[order[-1]]
            total = sum((rects[i].width() if horiz else rects[i].height()) for i in order)
            span = (last.right() - first.left()) if horiz else (last.bottom() - first.top())
            g = (span - total) / (len(order) - 1)
            cursor = first.left() if horiz else first.top()
            for i in order:
                r = rects[i]
                if horiz:
                    self._shift(objs[i], cursor - r.left(), 0)
                    cursor += r.width() + g
                else:
                    self._shift(objs[i], 0, cursor - r.top())
                    cursor += r.height() + g
        elif how == "vgap":
            if len(idx) < 2:
                self.statusBar().showMessage("2つ以上選択してください")
                return
            order = sorted(idx, key=lambda i: rects[i].center().y())
            cursor = rects[order[0]].bottom() + gap
            for i in order[1:]:
                r = rects[i]
                self._shift(objs[i], 0, cursor - r.top())
                cursor += r.height() + gap
        self._sync_pos()
        self.schedule()

    # ---------- 座布団 ----------
    def _fit_rect(self):
        block = R.block_rect(self.t)
        bx = self.t["box"]
        return block.adjusted(-bx["padx"], -bx["pady"], bx["padx"], bx["pady"]), block

    def _box_mode(self):
        if self.loading:
            return
        free = self.rb_free.isChecked()
        if free and self.t["box"].get("mode") != "free":
            r, _ = self._fit_rect()
            self.t["box"]["rect"] = [r.center().x(), r.center().y(), r.width(), r.height()]
        self.t["box"]["mode"] = "free" if free else "fit"
        self.load_ui()
        self.schedule()

    def _box_place(self, how):
        W, H = self.doc["canvas"]
        rect = self.t["box"]["rect"]
        if how == "hcenter":
            rect[0] = W / 2
        elif how == "full":
            rect[0], rect[2] = W / 2, W
        elif how == "fit":
            r, _ = self._fit_rect()
            self.t["box"]["rect"] = [r.center().x(), r.center().y(), r.width(), r.height()]
        self.load_ui()
        self.schedule()

    def _text_in_box(self, how):
        if not self.t["text"].strip():
            return
        cx, cy, bw, bh = self.t["box"]["rect"]
        _, block = self._fit_rect()
        inset = self.sp_inset.value()
        self.settings["box_inset"] = inset
        bcx, bcy = block.center().x(), block.center().y()
        if how == "left":
            bcx = cx - bw / 2 + inset + block.width() / 2
        elif how == "right":
            bcx = cx + bw / 2 - inset - block.width() / 2
        elif how == "center":
            bcx = cx
        elif how == "vcenter":
            bcy = cy
        R.set_block_center(self.t, bcx, bcy)
        if how in ("left", "right", "center"):
            self.t["font"]["align"] = how
        self.load_ui()
        self.schedule()

    # ---------- 描画 ----------
    def schedule(self):
        self.dirty = True
        self.timer.start()

    # ---------- 取り消し / やり直し ----------
    HIST_MAX = 200

    def _hist_record(self):
        """描画のたびに、内容が変わっていれば履歴に積む（スライダーの連続変更は1回分にまとまる）"""
        if getattr(self, "_hist_lock", False) or getattr(self, "_font_orig", None) is not None:
            return
        snap = json.dumps(self.doc, ensure_ascii=False, sort_keys=True)
        if getattr(self, "_hist_doc", None) is not self.doc:      # 新規・開く などで別のタイトルになった
            self._hist_doc = self.doc
            self._hist = [(snap, self.cur)]
            self._hist_i = 0
            return
        if snap == self._hist[self._hist_i][0]:
            return
        del self._hist[self._hist_i + 1:]
        self._hist.append((snap, self.cur))
        if len(self._hist) > self.HIST_MAX:
            del self._hist[0]
        self._hist_i = len(self._hist) - 1

    # ---------- 数値欄: ドラッグで変更 ----------
    def _setup_scrub(self):
        """数値欄の▲▼をやめて、左右ドラッグで値を変えられるようにする（クリックだけなら打ち込み）"""
        self._scrubs = set()
        self._scrub = None
        for sp in self.findChildren(QAbstractSpinBox):
            sp.setButtonSymbols(QAbstractSpinBox.NoButtons)
            sp.setFocusPolicy(Qt.StrongFocus)
            sp.setToolTip((sp.toolTip() + "\n" if sp.toolTip() else "")
                          + "左右にドラッグで変更（⇧で10倍・⌥で細かく）／クリックで入力")
            le = sp.lineEdit()
            le.setCursor(Qt.SizeHorCursor)
            sp.setCursor(Qt.SizeHorCursor)
            self._scrubs.add(le)
            self._scrubs.add(sp)

    def _scrub_event(self, obj, ev):
        if isinstance(obj, QAbstractSpinBox):
            sp, le = obj, obj.lineEdit()
            if ev.type() in (QEvent.FocusIn, QEvent.FocusOut):
                return False
        else:
            le, sp = obj, obj.parent()
        t = ev.type()
        if t == QEvent.FocusIn:
            le.setCursor(Qt.IBeamCursor)
            return False
        if t == QEvent.FocusOut:
            le.setCursor(Qt.SizeHorCursor)
            return False
        if t == QEvent.Wheel and not sp.hasFocus():
            par = sp.parentWidget()                 # 触っていない数値欄はホイールで変えない（画面のスクロールへ）
            if par is not None:
                QApplication.sendEvent(par, ev)
            return True
        if not sp.isEnabled():
            return False
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            # 入力欄にフォーカスがあってもドラッグなら値を変える（動かさなければ文字入力）
            self._scrub = {"sp": sp, "x0": ev.globalPosition().x(), "v0": sp.value(), "moved": False,
                           "focused": sp.hasFocus(), "pos": ev.position()}
            return True
        d = self._scrub
        if d is None or d["sp"] is not sp:
            return False
        if t == QEvent.MouseMove:
            dx = ev.globalPosition().x() - d["x0"]
            if not d["moved"] and abs(dx) < 3:
                return True
            d["moved"] = True
            m = ev.modifiers()
            f = 10.0 if m & Qt.ShiftModifier else (0.1 if m & Qt.AltModifier else 1.0)
            steps = dx / 3.0 * f                    # 3px で1目盛り
            step = sp.singleStep()
            if isinstance(sp, QSpinBox):
                v = d["v0"] + int(round(steps)) * step
            else:
                v = round(d["v0"] + steps * step, sp.decimals())
            if v != sp.value():
                sp.setValue(v)
                if not sp.keyboardTracking() and sp.hasFocus():
                    sp.editingFinished.emit()
            return True
        if t == QEvent.MouseButtonRelease:
            self._scrub = None
            if not d["moved"]:
                if d["focused"]:                    # 入力中のクリックはカーソル位置へ
                    le.setCursorPosition(le.cursorPositionAt(d["pos"].toPoint()))
                else:
                    sp.setFocus(Qt.MouseFocusReason)
                    sp.selectAll()
            else:
                le.setCursor(Qt.IBeamCursor if sp.hasFocus() else Qt.SizeHorCursor)
            return True
        if t == QEvent.MouseButtonDblClick:
            return True
        return False

    def eventFilter(self, obj, ev):
        """⌘Z / ⇧⌘Z は、文字入力欄以外（数値欄など）ではタイトラー全体の取り消しにする"""
        if obj in getattr(self, "_scrubs", ()):
            try:
                return self._scrub_event(obj, ev)
            except Exception:
                traceback.print_exc()
                return False
        if ev.type() == QEvent.ShortcutOverride:
            try:
                if ev.matches(QKeySequence.Undo) or ev.matches(QKeySequence.Redo):
                    fw = QApplication.focusWidget()
                    if fw is not getattr(self, "txt", None):
                        return True     # 部品に横取りさせず、メニューのショートカットを動かす
            except Exception:
                pass
        return False

    def act_undo(self):
        self._hist_go(-1)

    def act_redo(self):
        self._hist_go(1)

    def _hist_go(self, step):
        if getattr(self, "_hist_doc", None) is not self.doc:
            return
        if self.timer.isActive():   # まだ描画していない変更を先に履歴へ
            self.timer.stop()
            self._hist_record()
        j = self._hist_i + step
        if not (0 <= j < len(self._hist)):
            self.statusBar().showMessage("これ以上取り消せません" if step < 0 else "これ以上やり直せません")
            return
        # 取り消すときは「その変更をしたテロップ」を選び直す
        cur = self._hist[self._hist_i if step < 0 else j][1]
        self._hist_i = j
        self._hist_lock = True
        try:
            self.doc = json.loads(self._hist[j][0])
            self._hist_doc = self.doc
            n = len(self.doc["objects"])
            self.cur = max(0, min(cur, n - 1))
            self.sel = {self.cur}
            self._refresh_obj_list()
            self.load_ui()
            self.do_render()
        finally:
            self._hist_lock = False
        self.statusBar().showMessage(("取り消しました" if step < 0 else "やり直しました")
                                     + f"（{j}/{len(self._hist) - 1}）")

    def do_render(self):
        self._hist_record()
        try:
            self._roll_info()
        except Exception:
            traceback.print_exc()
        try:
            img, infos = R.render_doc(self.doc, self.rcache)
            self.last_infos = infos
            self._last_img = img
            self._show_preview()
        except Exception as e:
            traceback.print_exc()
            self.statusBar().showMessage(f"描画エラー: {e}")

    def _show_preview(self):
        img = getattr(self, "_last_img", None)
        if img is None:
            return
        self.preview.pivot = self._pivot_now()
        free = [bool(o["box"].get("enabled") and o["box"].get("mode") == "free") for o in self.doc["objects"]]
        o = self.t
        self.preview.cur_image = R.is_image(o)
        self.preview.xf_ctx = (tuple(o["pos"]), dict(o.get("xf") or {}))
        if (self.char_sel or self.preview.char_mode) and getattr(self, "char_uid", None) != o.get("uid"):
            self.preview.exit_char_mode(emit=False)        # 別のテロップに移ったら文字選択は終わり
            self._set_char_sel(None, sync_txt=False)
        try:
            L = R.text_layout(o)
            self.preview.char_boxes = L["chars"]
            self.preview.line_starts = L["line_starts"]
        except Exception:
            self.preview.char_boxes = []
        if not (o.get("text") or "") and not R.is_image(o):
            b = R.block_rect(o)
            sz = float(o["font"]["size"])
            cx, cy = b.center().x(), b.center().y()
            self.preview.empty_caret = (QPointF(cx, cy - sz / 2), QPointF(cx, cy + sz / 2))
        else:
            self.preview.empty_caret = None
        self.preview.set_image(img, self.last_infos, tuple(self.doc["canvas"]), free, self.sel, self.cur)

    # ---------- メッセージ ----------
    def err(self, e, title="エラー"):
        traceback.print_exc()
        msg = str(e)
        self.statusBar().showMessage(msg)
        QMessageBox.warning(self, title, msg)

    def bring_front(self):
        self.show()
        self.setWindowState((self.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        self.raise_()
        self.activateWindow()

    def _update_mode(self):
        if self.editing:
            n = len(self.doc["objects"])
            self.lbl_mode.setText(f"編集中: {self.editing['gid']}  (版 {self.editing['rev']} / {n}テロップ)")
            self.btn_place.setText(keys("更新して差し替え (⌘↩)"))
            self.btn_newplace.show()
        else:
            self.lbl_mode.setText("新規タイトル")
            self.btn_place.setText(keys("Resolveに配置 (⌘↩)"))
            self.btn_newplace.hide()
        self.btn_pending.hide()
        self._out_changed()

    # ---------- ファイル ----------
    def _folder(self, project):
        root = self.settings.get("save_root") or SAVE_ROOT
        safe = re.sub(r'[\\/:*?"<>|]', "_", project or "NoProject").strip() or "NoProject"
        d = os.path.join(root, safe)
        os.makedirs(d, exist_ok=True)
        return d

    def _new_gid(self):
        first = (self.doc["objects"][0]["text"].strip().splitlines() or ["title"])[0]
        snip = re.sub(r'[\\/:*?"<>|\s_]', "", first)[:12] or "title"
        return f"{snip}-{datetime.datetime.now().strftime('%y%m%d-%H%M%S')}"

    def _save_sidecar(self, folder, gid, rev, mode, output, pngs=()):
        model.save_json(os.path.join(folder, f"{gid}.ctitle.json"),
                        {"app": "ClapsTitler", "version": 2, "rev": rev, "mode": mode, "output": output,
                         "pngs": [os.path.basename(p) for p in pngs], "doc": self.doc})
        self.index[gid] = folder
        model.save_json(INDEX_PATH, self.index)

    def _placeholder(self):
        """Text+ 出力用の透明な下地画像（タイムライン解像度）"""
        W, H = (int(v) for v in self.doc["canvas"])
        path = os.path.join(SUPPORT, f"ClapsTitler_base_{W}x{H}.png")
        if not os.path.exists(path):
            img = QImage(W, H, QImage.Format_ARGB32)
            img.fill(0)
            os.makedirs(SUPPORT, exist_ok=True)
            img.save(path, "PNG")
        return path

    def _write(self, folder, gid, rev, mode):
        """PNGを書き出してサイドカーを保存。-> [(uid or None, pngパス), ...]（下のテロップから）"""
        out = []
        if mode == "single":
            img, _ = R.render_doc(self.doc, self.rcache)
            png = os.path.join(folder, png_name(gid, rev))
            if not img.save(png, "PNG"):
                raise RuntimeError(f"PNGを保存できませんでした: {png}")
            out.append((None, png))
        else:
            for o in self.doc["objects"]:
                img, _ = R.render(o)
                png = os.path.join(folder, png_name(gid, rev, o["uid"]))
                if not img.save(png, "PNG"):
                    raise RuntimeError(f"PNGを保存できませんでした: {png}")
                out.append((o["uid"], png))
        model.save_json(os.path.join(folder, f"{gid}.ctitle.json"),
                        {"app": "ClapsTitler", "version": 2, "rev": rev, "mode": mode,
                         "pngs": [os.path.basename(p) for _, p in out], "doc": self.doc})
        self.index[gid] = folder
        model.save_json(INDEX_PATH, self.index)
        return out

    def _find_folder(self, gid):
        f = self.index.get(gid)
        if f and os.path.exists(os.path.join(f, f"{gid}.ctitle.json")):
            return f
        root = self.settings.get("save_root") or SAVE_ROOT
        for dirpath, _, files in os.walk(root):
            if f"{gid}.ctitle.json" in files:
                self.index[gid] = dirpath
                return dirpath
        return None

    # ---------- 起動・終了 ----------
    def startup(self, msg):
        restored = False
        if msg != "edit-selected":
            restored = self._restore_session()
        try:
            info = self.bridge.info()
            if not restored:
                self.doc = model.new_doc(info["width"], info["height"], self.styles.get("__default__"))
                self.doc["mode"] = self.settings.get("mode", "separate")
                self.cur, self.sel = 0, {0}
                self.load_ui()
            self.statusBar().showMessage(f"Resolve接続: {info['project']} / {info['timeline']}")
        except Exception as e:
            self.statusBar().showMessage(f"Resolve未接続: {e}")
        self.watch.start()
        if msg == "edit-selected":
            self.act_edit_selected()
            return
        if msg == "new":
            self.act_new()
            return
        self.act_grab(quiet=True)
        self.do_render()
        if not restored:
            self.dirty = False

    def _save_session(self):
        ed = None
        if self.editing:
            ed = {k: self.editing[k] for k in ("gid", "rev", "folder", "mode")}
        try:
            model.save_json(SESSION_PATH, {"doc": self.doc, "editing": ed, "dirty": self.dirty,
                                           "cur": self.cur})
            self.settings["follow"] = self.chk_follow.isChecked()
            self.settings["duration"] = self.sp_dur.value()
            model.save_json(SETTINGS_PATH, self.settings)
        except Exception:
            traceback.print_exc()

    def _restore_session(self):
        data = model.load_json(SESSION_PATH)
        if not data:
            return False
        try:
            self.doc = model.normalize_doc(data.get("doc") or data.get("title"))
        except Exception:
            traceback.print_exc()
            return False
        self.cur = min(int(data.get("cur", 0)), len(self.doc["objects"]) - 1)
        self.sel = {self.cur}
        ed = data.get("editing")
        if ed and ed.get("gid") and os.path.exists(os.path.join(ed["folder"], f"{ed['gid']}.ctitle.json")):
            self.editing = dict(ed, mpis={})
        self._update_mode()
        self.load_ui()
        self.do_render()
        self.dirty = bool(data.get("dirty"))
        self.statusBar().showMessage("前回の状態を復元しました")
        return True

    def closeEvent(self, e):
        """閉じても終了せず隠すだけ（Resolveのメニュー・Dock・⌘Tabですぐ戻せる）。⌘Qで終了"""
        self._save_session()
        if getattr(self, "_quitting", False):
            e.accept()
            return
        e.ignore()
        if IS_WIN:
            self.showMinimized()        # Windows は Dock が無いので、隠さずタスクバーにしまう
        else:
            self.hide()

    def quit_app(self):
        self._quitting = True
        self._save_session()
        QApplication.quit()

    # ---------- タイムラインの選択に追従 ----------
    def _follow_toggled(self, v):
        self.settings["follow"] = v
        if not v:
            self.btn_pending.hide()

    def _poll_playhead(self):
        """再生ヘッドが動いて止まったら背景を取り直す"""
        if not self.chk_autobg.isChecked() or not self.isVisible():
            return
        try:
            tl = self.bridge.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
            key = (tl.GetName(), tl.GetCurrentTimecode())
        except Exception:
            return
        if key != getattr(self, "_ph_last", None):
            self._ph_last, self._ph_stable = key, 0          # 動いている
            return
        self._ph_stable = getattr(self, "_ph_stable", 0) + 1
        if self._ph_stable == 1 and key != getattr(self, "_bg_key", None):   # 止まった
            self._bg_key = key
            self.act_grab(quiet=True)

    def _poll_selection(self):
        self._poll_playhead()
        if not self.chk_follow.isChecked():
            return
        gid = self.bridge.peek_selected()
        if not gid or gid == self.last_seen:
            return
        self.last_seen = gid
        if self.editing and self.editing["gid"] == gid:
            return
        if self.dirty:
            self._pending = gid
            self.btn_pending.setText(f"選択中のタイトルを開く（今の変更は未反映）: {gid[:24]}")
            self.btn_pending.show()
            return
        self.load_group(gid, grab=True)

    def _open_pending(self):
        if getattr(self, "_pending", None):
            self.load_group(self._pending, grab=True)

    def load_group(self, gid, grab=False):
        folder = self._find_folder(gid)
        data = model.load_json(os.path.join(folder, f"{gid}.ctitle.json")) if folder else None
        if not data:
            self.statusBar().showMessage(f"編集データが見つかりません: {gid}.ctitle.json")
            return False
        self.doc = model.doc_from_sidecar(data)
        self.doc["mode"] = data.get("mode", self.doc.get("mode", "single"))
        self.cur, self.sel = 0, {0}
        self.editing = {"gid": gid, "rev": int(data.get("rev", 1)), "folder": folder,
                        "mode": self.doc["mode"], "output": data.get("output", "png"), "mpis": {}}
        self._update_mode()
        self.load_ui()
        self.do_render()
        self.dirty = False
        if grab:
            self.act_grab(quiet=True)
        self.statusBar().showMessage(f"読み込みました: {gid}")
        return True

    # ---------- アクション ----------
    def on_message(self, server):
        conn = server.nextPendingConnection()
        if conn is None:
            return
        conn.waitForReadyRead(1000)
        msg = bytes(conn.readAll()).decode("utf-8", "ignore").strip()
        conn.disconnectFromServer()
        self.bring_front()
        if msg == "edit-selected":
            self.act_edit_selected()
        elif msg == "new":
            self.act_new()
        else:
            self.act_grab(quiet=True)

    def act_grab(self, quiet=False):
        try:
            gid = self.editing["gid"] if self.editing else None
            self.bridge.grab_frame(BG_PATH, hide_gid=gid)
            img = QImage(BG_PATH)
            if img.isNull():
                raise RuntimeError("背景画像を読み込めませんでした")
            self.preview.bg = img
            try:
                tl = self.bridge.resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
                self._bg_key = (tl.GetName(), tl.GetCurrentTimecode())
            except Exception:
                pass
            self.cmb_bg.setCurrentIndex(0)
            self.preview.update()
            self.statusBar().showMessage("背景を取得しました")
        except Exception as e:
            if quiet:
                self.statusBar().showMessage(f"背景取得できず: {e}")
            else:
                self.err(e)

    def act_new(self):
        W, H = self.doc["canvas"]
        try:
            info = self.bridge.info()
            W, H = info["width"], info["height"]
        except Exception:
            pass
        style = model.extract_style(self.t)
        style["box"]["mode"] = "fit"
        self.doc = model.new_doc(W, H, style)
        self.doc["objects"][0]["text"] = ""
        self.doc["mode"] = self.settings.get("mode", "separate")
        self.cur, self.sel = 0, {0}
        self.editing = None
        j = self.cmb_out.findData(self.settings.get("output", "textplus"))   # 新規は既定の出力（Text+）から
        self.cmb_out.setCurrentIndex(max(0, j))
        self._update_mode()
        self.load_ui()
        self.do_render()
        self.dirty = False
        self.act_grab(quiet=True)
        self.txt.setFocus()

    def act_open(self):
        start = self.settings.get("save_root") or SAVE_ROOT
        path, _ = QFileDialog.getOpenFileName(self, "タイトルを開く（複製として）", start, "CT Titler タイトル (*.ctitle.json)")
        if not path:
            return
        data = model.load_json(path)
        if not data or ("title" not in data and "doc" not in data):
            self.err("タイトルファイルを読み込めませんでした")
            return
        self.doc = model.doc_from_sidecar(data)
        for o in self.doc["objects"]:
            o["uid"] = model.new_uid()
        self.cur, self.sel = 0, {0}
        self.editing = None
        self._update_mode()
        self.load_ui()
        self.do_render()

    def act_edit_selected(self):
        try:
            gid = self.bridge.selected_title()
            self.last_seen = gid
            if not self.load_group(gid):
                raise RuntimeError("編集データ(.ctitle.json)が見つかりません")
            self.bring_front()
            self.act_grab(quiet=True)
        except Exception as e:
            self.bring_front()
            self.err(e, "編集に戻れません")

    def act_import_textplus(self, how="append"):
        try:
            info = self.bridge.info()
            tl_name, tps = self.bridge.read_textplus(textplus.ELEMENT_KEYS, textplus.TOOL_KEYS)
            empty = not any(model.has_content(o) for o in self.doc["objects"])
            if how == "append" and not empty:
                canvas = list(self.doc["canvas"])
                objs = [textplus.convert(tp, canvas) for tp in tps]
                for o in objs:
                    o = model.normalize(o)
                    o["canvas"] = canvas
                    self.doc["objects"].append(o)
                n = len(self.doc["objects"])
                self.cur = n - 1
                self.sel = set(range(n - len(objs), n))
                self.load_ui()
                self.schedule()
                self.statusBar().showMessage(f"Text+ を {len(objs)} 個追加しました（{tps[0]['clip']['name']}）")
                return
            canvas = [info["width"], info["height"]]
            objs = [textplus.convert(tp, canvas) for tp in tps]
            self.doc = model.normalize_doc({"version": 2, "canvas": canvas, "mode": "single", "objects": objs})
            clip = tps[0]["clip"]
            self.doc["source"] = {"timeline": tl_name, "name": clip["name"],
                                  "start": clip["start"], "dur": clip["dur"], "track": clip["track"]}
            if len(objs) > 1:
                self.doc["mode"] = "separate"
            self.cur, self.sel = 0, {0}
            self.editing = None
            self._update_mode()
            self.load_ui()
            self.do_render()
            self.dirty = True
            self.lbl_mode.setText(f"Text+から取り込み: {clip['name']}（{len(objs)}テロップ）")
            self.statusBar().showMessage("取り込みました。「Resolveに配置」で元のクリップと同じ位置・尺に置きます（元のText+はそのまま残ります）")
        except Exception as e:
            self.err(e, "Text+を取り込めません")

    def act_place(self):
        if not any(model.has_content(o) for o in self.doc["objects"]):
            self.statusBar().showMessage("テロップの文字が空なので、置くものがありません")
            return
        if self.editing:
            self.act_update()
            return
        try:
            info = self.bridge.info()
            folder = self._folder(info["project"])
            gid = self._new_gid()
            mode = self.doc.get("mode", "single")
            output = self.cmb_out.currentData()
            if output == "textplus" and mode == "single" and len(self.doc["objects"]) > 1:
                r = QMessageBox.question(
                    self, "配置方法",
                    f"テロップが {len(self.doc['objects'])} つあります。\n"
                    "テロップごとに別の Text+ タイトルとして置きますか？\n\n"
                    "「はい」: 別々のタイトル（それぞれ Inspector で触れます）\n"
                    "「いいえ」: 1つのタイトルにまとめる（Inspector に出るのは1つ目だけ）",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel, QMessageBox.Yes)
                if r == QMessageBox.Cancel:
                    return
                if r == QMessageBox.Yes:
                    mode = self.doc["mode"] = "separate"
                    self.cmb_mode.setCurrentIndex(self.cmb_mode.findData("separate"))
            secs, track = self.sp_dur.value(), self.cmb_track.currentData()
            comp_name = COMPOUND_PREFIX + gid if mode == "compound" else None
            src = self.doc.get("source")
            rec = dur = None
            if src and src.get("timeline") == info.get("timeline"):
                rec, dur = src["start"], src["dur"]          # 元のText+と同じ位置・尺
            if output == "textplus":
                self.doc.pop("source", None)
                self._save_sidecar(folder, gid, 1, mode, output)
                groups = [None] if mode == "single" else [o["uid"] for o in self.doc["objects"]]
                doc = copy.deepcopy(self.doc)
                items, comp = self.bridge.place_titles(
                    self._placeholder(), groups, secs, track,
                    lambda c, uid: TO.build(c, doc, gid, only_uid=uid), comp_name, rec, dur)
                self.editing = {"gid": gid, "rev": 1, "folder": folder, "mode": mode,
                                "output": output, "mpis": {}}
                self._update_mode()
                self.settings["duration"] = secs
                model.save_json(SETTINGS_PATH, self.settings)
                self.dirty = False
                self.last_seen = gid
                self.statusBar().showMessage(f"Text+で配置しました: {gid}（{len(items)}クリップ"
                                             + ("→複合クリップ" if comp else "") + "）")
                return
            pngs = self._write(folder, gid, 1, mode)
            mpis, comp = self.bridge.place_many([p for _, p in pngs], secs, track, comp_name, rec, dur)
            self.doc.pop("source", None)
            self.editing = {"gid": gid, "rev": 1, "folder": folder, "mode": mode, "output": "png",
                            "mpis": {uid: m for (uid, _), m in zip(pngs, mpis)}}
            self._update_mode()
            self.settings["duration"] = secs
            model.save_json(SETTINGS_PATH, self.settings)
            self.dirty = False
            self.last_seen = gid
            self.statusBar().showMessage(f"配置しました: {gid}（{len(pngs)}クリップ"
                                         + ("→複合クリップ" if comp else "") + "）")
        except Exception as e:
            self.err(e, "配置できません")

    def act_place_new(self):
        self.editing = None
        for o in self.doc["objects"]:
            o["uid"] = model.new_uid()
        self._update_mode()
        self.act_place()

    def act_update_textplus(self):
        ed = self.editing
        try:
            gid, mode = ed["gid"], ed["mode"]
            found = self.bridge.find_textplus_items(gid)
            if not found:
                raise ResolveError("今のタイムラインに、このタイトル（Text+）が見つかりません")
            doc = copy.deepcopy(self.doc)
            uids_now = [o["uid"] for o in doc["objects"]]
            present, removed = set(), 0
            for item, tags in found:
                if mode == "single":
                    self.bridge.rebuild_textplus(item, lambda c: TO.build(c, doc, gid))
                    present.update(uids_now)
                else:
                    u = tags[0]
                    if u in uids_now:
                        self.bridge.rebuild_textplus(item, lambda c, u=u: TO.build(c, doc, gid, only_uid=u))
                        present.add(u)
                    else:
                        removed += 1
            new = [u for u in uids_now if u not in present]
            if new and mode != "single":
                self.bridge.place_titles(self._placeholder(), new, self.sp_dur.value(), 0,
                                         lambda c, uid: TO.build(c, doc, gid, only_uid=uid))
            ed["rev"] += 1
            self._save_sidecar(ed["folder"], gid, ed["rev"], mode, "textplus")
            self.dirty = False
            self._update_mode()
            msg = f"更新しました（版 {ed['rev']} / Text+ {len(found)}クリップ）"
            if new and mode != "single":
                msg += f" / 追加テロップ {len(new)} 個を再生ヘッド位置に配置"
            if removed:
                msg += f" / 削除したテロップのクリップ {removed} 個はタイムラインに残っています"
            self.statusBar().showMessage(msg)
        except Exception as e:
            self.err(e, "更新できません")

    def act_convert(self):
        """PNG⇔Text+ の変換: 元のクリップと同じ位置・尺・トラック付近に新しく置き、元のクリップは無効化する"""
        ed = self.editing
        try:
            old = self.bridge.find_items_by_gid(ed["gid"])
            if not old:
                raise ResolveError("今のタイムラインに、このタイトルが見つかりません")
            start = min(it.GetStart() for it in old)
            dur = max(it.GetEnd() for it in old) - start
            self.doc["source"] = {"timeline": self.bridge.info().get("timeline"), "start": start, "dur": dur}
            self.editing = None
            for o in self.doc["objects"]:
                o["uid"] = model.new_uid()
            self.act_place()
            if self.editing:      # 置けたら元のクリップを無効化（削除はしない）
                for it in old:
                    try:
                        it.SetClipEnabled(False)
                    except Exception:
                        pass
                self.statusBar().showMessage(self.statusBar().currentMessage()
                                             + f" / 元のクリップ {len(old)} 個は無効化しました（不要なら削除してください）")
        except Exception as e:
            self.err(e, "変換できません")

    def act_update(self):
        ed = self.editing
        if self._needs_convert():
            self.act_convert()
            return
        if ed.get("output") == "textplus":
            self.act_update_textplus()
            return
        try:
            mode = ed["mode"]
            old_rev, rev = ed["rev"], ed["rev"] + 1
            olds = {}
            for f in os.listdir(ed["folder"]):
                r = parse_png(f)
                if r and r[0] == ed["gid"] and r[2] == old_rev:
                    olds[r[1]] = os.path.join(ed["folder"], f)
            pngs = self._write(ed["folder"], ed["gid"], rev, mode)
            new_clips, missing = [], 0
            for uid, png in pngs:
                mpi = ed["mpis"].get(uid)
                if mpi is None and uid in olds:
                    mpi = self.bridge.find_mpi(olds[uid])
                if mpi is None:
                    if uid in olds:
                        missing += 1
                    else:
                        new_clips.append((uid, png))
                    continue
                self.bridge.replace(mpi, png)
                ed["mpis"][uid] = mpi
            if new_clips:     # 後から追加したテロップは再生ヘッド位置に新しく置く
                mpis, _ = self.bridge.place_many([p for _, p in new_clips], self.sp_dur.value(), 0, None)
                for (uid, _), m in zip(new_clips, mpis):
                    ed["mpis"][uid] = m
            for p in olds.values():
                try:
                    os.remove(p)
                except OSError:
                    pass
            ed["rev"] = rev
            self.dirty = False
            self._update_mode()
            msg = f"更新しました（版 {rev}）"
            if new_clips:
                msg += f" / 追加テロップ {len(new_clips)} 個を再生ヘッド位置に配置"
                if mode == "compound":
                    msg += "（複合クリップの外）"
            if missing:
                msg += f" / 見つからないクリップ {missing}"
            removed = set(olds) - {u for u, _ in pngs}
            if removed and mode != "single":
                msg += f" / 削除したテロップのクリップ {len(removed)} 個はタイムラインに残っています"
            self.statusBar().showMessage(msg)
        except Exception as e:
            self.err(e, "更新できません")

    def act_export_png(self):
        path, _ = QFileDialog.getSaveFileName(self, "PNG書き出し", os.path.join(os.path.expanduser("~"), "Desktop", "title.png"), "PNG (*.png)")
        if path:
            img, _ = R.render_doc(self.doc, self.rcache)
            img.save(path, "PNG")
            self.statusBar().showMessage(f"書き出しました: {path}")

    # ---------- スタイル ----------
    def _sample_text(self):
        s = (self.settings.get("sample") or "").strip()
        return "あ" if s in ("", "見本 Sample") else s     # 1文字を大きく見せる

    def _thumb(self, name):
        style = self.styles[name]
        key = json.dumps(style, sort_keys=True, ensure_ascii=False) + self._sample_text()
        if key in self.thumb_cache:
            return self.thumb_cache[key]
        try:
            t = thumb_title(style, self._sample_text())
            img, info = R.render(t)
            W, H = t["canvas"]
            S = THUMB_PX
            pm = QPixmap(S, S)
            pm.fill(QColor("#555555"))
            p = QPainter(pm)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            n = 10
            for y in range(0, S, n):
                for x in range(0, S, n):
                    if (x // n + y // n) % 2:
                        p.fillRect(x, y, n, n, QColor("#666666"))
            r = _alpha_bounds(img) or (info["text"].united(info["box"]) if info.get("box") is not None
                                         else info["text"])
            r = r.adjusted(-8, -8, 8, 8).intersected(QRectF(0, 0, W, H))
            r = _square(r)          # 正方形に広げて、縦横の比率を崩さない
            sc = min(S / max(1, r.width()), S / max(1, r.height()))
            dw, dh = r.width() * sc, r.height() * sc
            p.drawImage(QRectF((S - dw) / 2, (S - dh) / 2, dw, dh), img, r)
            p.end()
            icon = QIcon()
            for mode in (QIcon.Normal, QIcon.Selected, QIcon.Active):   # 選択中も色を変えずに表示
                icon.addPixmap(pm, mode)
        except Exception:
            traceback.print_exc()
            icon = QIcon()
        self.thumb_cache[key] = icon
        return icon

    def _style_names(self):
        """登録順（古い順）。辞書の順番がそのまま登録順"""
        return [k for k in self.styles if not k.startswith("__")]

    def _refresh_styles(self, select=None):
        self.lst_styles.blockSignals(True)
        self.lst_styles.clear()
        for k in self._style_names():
            it = QListWidgetItem(self._thumb(k), k)
            it.setTextAlignment(Qt.AlignHCenter)
            it.setData(Qt.UserRole, k)
            it.setFlags(it.flags() | Qt.ItemIsEditable)
            self.lst_styles.addItem(it)
            if k == select:
                self.lst_styles.setCurrentItem(it)
        self.lst_styles.blockSignals(False)
        if select:
            cur = self.lst_styles.currentItem()
            if cur:
                self.lst_styles.scrollToItem(cur)

    def _save_styles(self, select=None):
        model.save_json(STYLES_PATH, self.styles)
        self._refresh_styles(select)

    def _style_item(self, it=None):
        it = it or self.lst_styles.currentItem()
        if it and it.data(Qt.UserRole) in self.styles:
            return it, it.data(Qt.UserRole)
        return None, None

    def style_save(self):
        """名前を聞かずにすぐ登録（「スタイル 1」…）。名前はあとでダブルクリックで変えられる"""
        n = 1
        while f"スタイル {n}" in self.styles:
            n += 1
        name = f"スタイル {n}"
        self.styles[name] = model.extract_style(self.t)
        self._save_styles(select=name)
        self.statusBar().showMessage(f"「{name}」を登録しました（ダブルクリックで名前を変更）")

    def style_overwrite(self, it=None):
        it, name = self._style_item(it)
        if not name:
            self.statusBar().showMessage("上書きするタイルを選んでください")
            return
        self.styles[name] = model.extract_style(self.t)
        self._save_styles(select=name)
        self.statusBar().showMessage(f"「{name}」を今の装飾で上書きしました")

    def style_apply(self, it=None):
        it, name = self._style_item(it)
        if not name:
            return
        st = self.styles[name]
        if self.char_sel:
            self._style_to_chars(st, name)
            return
        for i in self.sel:
            o = self.doc["objects"][i]
            box_keep = copy.deepcopy(o["box"])
            o = model.apply_style(o, st, self.chk_keep_size.isChecked())
            if box_keep.get("mode") == "free":       # 固定した座布団の位置は保つ
                o["box"]["mode"], o["box"]["rect"] = "free", box_keep["rect"]
            o["canvas"] = list(self.doc["canvas"])
            self.doc["objects"][i] = o
        self.load_ui()
        self.schedule()
        self.statusBar().showMessage(f"「{name}」を適用しました（⌘Zで戻せます）")

    def _style_to_chars(self, st, name):
        """スタイル棚のスタイルを、選んだ文字だけに当てる（フォント・サイズ・面と縁の色）"""
        f = st.get("font") or {}
        m = {}
        if f.get("family"):
            fs = [f["family"], f.get("style", "")]
            main = [self.t["font"]["family"], self.t["font"].get("style", "")]
            m["font"] = None if fs == main else fs
        if not self.chk_keep_size.isChecked() and f.get("size"):
            k = round(float(f["size"]) / max(1.0, float(self.t["font"]["size"])), 4)
            m["size"] = None if abs(k - 1) < 1e-6 else k
        fc = st.get("face") or {}
        if fc.get("color"):
            m["face"] = fc["color"]
        # 縁・影の数値は、スタイルの文字サイズ → 当てる文字の大きさに合わせて縮尺
        ssize = float(f.get("size") or self.t["font"]["size"])
        kch = float(m["size"]) if m.get("size") else 1.0
        tsize = float(self.t["font"]["size"]) * (1.0 if self.chk_keep_size.isChecked() else kch)
        kk = tsize / max(1.0, ssize)
        ed, ew, es, eo = {}, {}, {}, {}
        for i, e in enumerate(st.get("edges") or []):
            if i >= len(self.t["edges"]):
                break
            on = bool(e.get("enabled") and e.get("width", 0) > 0)
            ew[str(i)] = round(float(e.get("width", 0)) * kk, 3) if on else 0.0
            if on:
                if e.get("color"):
                    ed[str(i)] = e["color"]
                es[str(i)] = round(float(e.get("soft", 0)) * kk, 3)
                eo[str(i)] = float(e.get("opacity", 1))
        m["edges"], m["ew"], m["es"], m["eo"] = ed, ew, es, eo
        ss = st.get("shadow") or {}
        if ss:
            m["sh_on"] = bool(ss.get("enabled"))
            m["sh_color"] = ss.get("color", self.t["shadow"]["color"])
            m["sh_blur"] = round(float(ss.get("blur", 0)) * kk, 3)
            m["sh_op"] = float(ss.get("opacity", 1))
            m["sh_dist"] = round(float(ss.get("distance", 0)) * kk, 3)
            m["sh_angle"] = float(ss.get("angle", 45))
        self._cs_apply("merge", m)
        self.statusBar().showMessage(f"「{name}」を選んだ文字に当てました"
                                     "（押し出しはテロップ全体の設定のまま）")

    def style_delete(self, it=None):
        it, name = self._style_item(it)
        if name and QMessageBox.question(self, "削除", f"「{name}」を削除しますか？") == QMessageBox.Yes:
            self.styles.pop(name, None)
            self._save_styles()

    def style_set_default(self):
        self.styles["__default__"] = model.extract_style(self.t)
        self._save_styles()
        self.statusBar().showMessage("新規作成時の初期値に設定しました")


# ------------------------------------------------------------ 起動
def _check_expiry():
    """検証版の使用期限。claps_titler/trial.py がある配布版だけ期限を確かめる（開発版は無期限）"""
    try:
        from . import trial
    except ImportError:
        return True
    today = datetime.date.today()
    last_day = datetime.date.fromisoformat(trial.LAST_DAY)
    # 時計を戻して使われないように、これまでに起動した一番新しい日付を覚えておく
    stamp = os.path.join(SUPPORT, ".trial")
    seen = today
    try:
        seen = max(today, datetime.date.fromisoformat(open(stamp).read().strip()))
    except Exception:
        pass
    try:
        os.makedirs(SUPPORT, exist_ok=True)
        with open(stamp, "w") as f:
            f.write(seen.isoformat())
    except Exception:
        pass
    left = (last_day - seen).days
    when = f"{last_day.year}年{last_day.month}月{last_day.day}日"
    if left < 0:
        QMessageBox.information(None, APP_NAME,
                                f"{APP_NAME} {APP_VERSION} の使用期限（{when}まで）が過ぎました。\n"
                                "新しい版を配布元から入手してください。")
        return False
    if left <= 7:
        QMessageBox.information(None, APP_NAME,
                                f"{APP_NAME} {APP_VERSION} は {when} まで使えます（あと {left + 1} 日）。")
    return True


def _selftest(out_dir):
    """自動テスト用: 画面を作って描画し、スクリーンショットと結果を書き出して終了する"""
    from PySide6.QtCore import QEventLoop
    os.makedirs(out_dir, exist_ok=True)
    res = {"ok": False}
    try:
        app = QApplication.instance() or QApplication(sys.argv[:1])
        n_lua = install_resolve_scripts()
        from .paths import RESOLVE_UTILITY, DEFAULT_FONT
        res["lua_written"] = n_lua
        res["lua_files"] = sorted(f for f in os.listdir(RESOLVE_UTILITY) if f.startswith("CT Titler"))
        with open(os.path.join(RESOLVE_UTILITY, "CT Titler 起動.lua"), encoding="utf-8") as fh:
            res["lua_sample"] = fh.read()
        win = MainWindow()
        for t in win.findChildren(QTimer):
            if t is not win.timer:
                t.stop()
        win.resize(1500, 950)
        win.show()
        doc = model.new_doc(1920, 1080)
        o = doc["objects"][0]
        o["text"] = "テロップ表示テスト\nWindows ＆ Mac"
        o["face"].update({"gradient": True, "stops": [[0, "#fff6c8", 1], [0.5, "#ffd34d", 1], [1, "#c27a12", 1]]})
        o["edges"][1].update({"enabled": True, "width": 10, "color": "#ffffff"})
        o["box"].update({"enabled": True, "opacity": 0.6})
        o["extrude"]["enabled"] = True
        win.doc = doc
        win.cur, win.sel = 0, {0}
        win._refresh_obj_list()
        win.load_ui()
        win.do_render()
        loop = QEventLoop()
        QTimer.singleShot(800, loop.quit)
        loop.exec()
        win.grab().save(os.path.join(out_dir, "window.png"))
        img, info = R.render(o)
        img.save(os.path.join(out_dir, "render.png"))
        f = R.make_font(o["font"])
        res.update({"ok": True, "font_wanted": list(DEFAULT_FONT), "font_used": f.family(),
                    "text_rect": [round(v, 1) for v in info["text"].getRect()],
                    "title": win.windowTitle(), "place_button": win.btn_place.text(),
                    "support": SUPPORT, "save_root": SAVE_ROOT})
        try:
            from .resolve_bridge import Bridge
            br = Bridge()
            res["resolve"] = br.resolve.GetProductName() if br.connect() else ("未接続: " + br.error)[:200]
        except Exception as e:
            res["resolve"] = f"エラー: {e}"
        try:
            from . import trial
            res["trial_last_day"] = trial.LAST_DAY
        except ImportError:
            res["trial_last_day"] = None
        win._hist_go(-1)
    except Exception:
        res["error"] = traceback.format_exc()
    with open(os.path.join(out_dir, "selftest.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    return 0 if res["ok"] else 1


def _mac_app_name(name):
    """Python のまま動かしたとき、Mac のメニューバーに「Python」と出るのを「CT Titler」にする"""
    import ctypes
    import ctypes.util
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        send0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        send_s = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(("objc_msgSend", objc))
        send2 = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        sel = objc.sel_registerName
        bundle = send0(objc.objc_getClass(b"NSBundle"), sel(b"mainBundle"))
        info = send0(bundle, sel(b"infoDictionary"))
        nsstr = objc.objc_getClass(b"NSString")
        val = send_s(nsstr, sel(b"stringWithUTF8String:"), name.encode("utf-8"))
        for key in (b"CFBundleName", b"CFBundleDisplayName"):
            k = send_s(nsstr, sel(b"stringWithUTF8String:"), key)
            send2(info, sel(b"setObject:forKey:"), val, k)
    except Exception:
        traceback.print_exc()


def _app_icon():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    return QIcon(p) if os.path.exists(p) else QIcon()


def main():
    setup_log()
    from .paths import IS_MAC, FROZEN
    if IS_MAC and not FROZEN:
        _mac_app_name(APP_NAME)
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        return _selftest(sys.argv[i + 1] if i + 1 < len(sys.argv) else "selftest_out")
    msg = "edit-selected" if "--edit-selected" in sys.argv else ("new" if "--new" in sys.argv else "show")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setWindowIcon(_app_icon())          # Dock／タスクバーのアイコン（Python のロケットの代わり）
    app.setQuitOnLastWindowClosed(False)
    sock = QLocalSocket()
    sock.connectToServer(SERVER)
    if sock.waitForConnected(300):          # 既に起動中ならそちらに伝えて終了
        sock.write(msg.encode())
        sock.flush()
        sock.waitForBytesWritten(1000)
        sock.disconnectFromServer()
        return 0
    if not _check_expiry():
        return 0
    install_resolve_scripts()
    QLocalServer.removeServer(SERVER)
    server = QLocalServer()
    server.listen(SERVER)
    win = MainWindow()
    try:
        from . import trial
        d = datetime.date.fromisoformat(trial.LAST_DAY)
        win.setWindowTitle(f"{APP_NAME} {APP_VERSION} — {APP_TAGLINE}（{d.year}/{d.month}/{d.day}まで）")
    except ImportError:
        pass
    server.newConnection.connect(lambda: win.on_message(server))

    def on_state(state):                    # Dockアイコンや⌘Tabで戻ったとき、隠れていたら出す
        if state == Qt.ApplicationActive and not win.isVisible():
            win.bring_front()
    app.applicationStateChanged.connect(on_state)
    win.show()
    QTimer.singleShot(150, lambda: win.startup(msg))
    return app.exec()
