"""多色グラデーションの編集バー
・バーをクリック → 色の点を追加（その位置の色で）
・点をドラッグ → 位置を移動 / 下へ大きく引き離す → 削除（2点は残す）
・点をダブルクリック → 色を選ぶ（変更がその場で反映）
・点を選んで Delete キー → 削除
"""
import copy

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QColorDialog, QDialog, QWidget


def lerp_color(stops, pos):
    """位置 pos の色を、前後の点から補間して返す (hex, alpha)"""
    st = sorted(stops, key=lambda s: s[0])
    if pos <= st[0][0]:
        return st[0][1], st[0][2]
    for a, b in zip(st, st[1:]):
        if a[0] <= pos <= b[0]:
            t = 0 if b[0] == a[0] else (pos - a[0]) / (b[0] - a[0])
            ca, cb = QColor(a[1]), QColor(b[1])
            c = QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                       round(ca.green() + (cb.green() - ca.green()) * t),
                       round(ca.blue() + (cb.blue() - ca.blue()) * t))
            return c.name(), a[2] + (b[2] - a[2]) * t
    return st[-1][1], st[-1][2]


class GradientEditor(QWidget):
    changed = Signal()
    selectionChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(58)
        self.setFocusPolicy(Qt.ClickFocus)
        self._stops = [[0.0, "#ffffff", 1.0], [1.0, "#ffd200", 1.0]]
        self.sel = 0
        self._drag = None
        self._away = False

    # ----- データ
    def stops(self):
        return sorted(copy.deepcopy(self._stops), key=lambda s: s[0])

    def setStops(self, stops):
        st = [[float(s[0]), QColor(s[1]).name(), float(s[2]) if len(s) > 2 else 1.0] for s in (stops or [])]
        if len(st) < 2:
            st = [[0.0, "#ffffff", 1.0], [1.0, "#ffd200", 1.0]]
        self._stops = st
        self.sel = min(self.sel, len(st) - 1)
        self.update()

    def selected(self):
        return self._stops[self.sel] if 0 <= self.sel < len(self._stops) else None

    def set_selected(self, pos=None, color=None, alpha=None):
        s = self.selected()
        if s is None:
            return
        if pos is not None:
            s[0] = max(0.0, min(1.0, float(pos)))
        if color is not None:
            s[1] = QColor(color).name()
        if alpha is not None:
            s[2] = max(0.0, min(1.0, float(alpha)))
        self.update()
        self.changed.emit()

    def reverse(self):
        for s in self._stops:
            s[0] = 1.0 - s[0]
        self.update()
        self.changed.emit()

    def remove_selected(self):
        if len(self._stops) > 2 and 0 <= self.sel < len(self._stops):
            del self._stops[self.sel]
            self.sel = max(0, self.sel - 1)
            self.update()
            self.changed.emit()
            self.selectionChanged.emit()

    # ----- 描画
    def _bar(self):
        return QRectF(10, 4, self.width() - 20, 26)

    def _x(self, pos):
        b = self._bar()
        return b.left() + pos * b.width()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        b = self._bar()
        # 市松（透明部分が見えるように）
        p.save()
        p.setClipRect(b)
        n = 6
        for y in range(int(b.height() // n) + 1):
            for x in range(int(b.width() // n) + 1):
                p.fillRect(QRectF(b.x() + x * n, b.y() + y * n, n, n),
                           QColor("#bbbbbb") if (x + y) % 2 else QColor("#888888"))
        p.restore()
        g = QLinearGradient(b.topLeft(), b.topRight())
        for s in self.stops():
            c = QColor(s[1])
            c.setAlphaF(s[2])
            g.setColorAt(s[0], c)
        p.fillRect(b, g)
        p.setPen(QPen(QColor("#555"), 1))
        p.drawRect(b)
        # 点（三角＋色の四角）
        for i, s in enumerate(self._stops):
            x = self._x(s[0])
            y = b.bottom() + 2
            tri = QPainterPath()
            tri.moveTo(x, y)
            tri.lineTo(x - 6, y + 8)
            tri.lineTo(x + 6, y + 8)
            tri.closeSubpath()
            selected = i == self.sel
            p.setPen(QPen(QColor("#2f6fd6") if selected else QColor("#333"), 2 if selected else 1))
            p.setBrush(QColor("#ffffff") if selected else QColor("#dddddd"))
            p.drawPath(tri)
            p.setBrush(QColor(s[1]))
            p.drawRect(QRectF(x - 6, y + 8, 12, 12))
        p.end()

    # ----- 操作
    def _hit(self, pos):
        b = self._bar()
        if pos.y() < b.bottom():
            return None
        best, bd = None, 9.0
        for i, s in enumerate(self._stops):
            d = abs(self._x(s[0]) - pos.x())
            if d < bd:
                best, bd = i, d
        return best

    def _pos_at(self, x):
        b = self._bar()
        return max(0.0, min(1.0, (x - b.left()) / max(1.0, b.width())))

    def mousePressEvent(self, e):
        pos = e.position()
        i = self._hit(pos)
        if i is not None:
            self.sel = i
            self._drag = i
            self._away = False
            self.selectionChanged.emit()
            self.update()
            return
        if self._bar().adjusted(0, 0, 0, 22).contains(pos):
            t = self._pos_at(pos.x())
            col, a = lerp_color(self._stops, t)
            self._stops.append([t, col, a])
            self.sel = len(self._stops) - 1
            self._drag = self.sel
            self.selectionChanged.emit()
            self.update()
            self.changed.emit()

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        pos = e.position()
        self._away = pos.y() > self.height() + 30 and len(self._stops) > 2
        self._stops[self._drag][0] = self._pos_at(pos.x())
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e):
        if self._drag is not None and self._away:
            self.sel = self._drag
            self.remove_selected()
        self._drag = None
        self._away = False

    def mouseDoubleClickEvent(self, e):
        i = self._hit(e.position())
        if i is None:
            return
        self.sel = i
        s = self._stops[i]
        old = s[1]
        dlg = QColorDialog(QColor(old), self)
        dlg.setWindowTitle("グラデーションの色")
        dlg.setOption(QColorDialog.DontUseNativeDialog, True)

        def live(c):
            s[1] = c.name()
            self.update()
            self.changed.emit()
        dlg.currentColorChanged.connect(live)
        if dlg.exec() != QDialog.Accepted:
            s[1] = old
        else:
            s[1] = dlg.selectedColor().name()
        self.update()
        self.changed.emit()
        self.selectionChanged.emit()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_selected()
        else:
            super().keyPressEvent(e)
