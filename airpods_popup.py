"""
AirPods Connect Popup for Windows
---------------------------------
Listens for Apple "proximity pairing" BLE advertisements (what AirPods broadcast
when the case lid is opened) and shows an iPhone-style popup with battery levels.

Install:   pip install bleak PySide6
Run:       python airpods_popup.py
Preview:   python airpods_popup.py --demo      (no AirPods needed)
"""
import asyncio
import sys
import threading
import time
from dataclasses import dataclass

from PySide6.QtCore import (QEasingCurve, QObject, QPropertyAnimation, QRectF, Qt,
                            QTimer, QUrl, Signal)
from PySide6.QtGui import (QAction, QColor, QDesktopServices, QFont, QGuiApplication,
                           QIcon, QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QMenu, QPushButton, QSystemTrayIcon,
                               QWidget)

try:
    import winreg  # Windows only
except ImportError:
    winreg = None

APP_NAME = "AirPodsPopup"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

APPLE_COMPANY_ID = 0x004C
RSSI_THRESHOLD = -62        # only trigger when AirPods are really close
COOLDOWN_SECONDS = 25       # don't re-popup immediately after dismissal
AUTO_HIDE_MS = 9000

MODELS = {
    0x0220: "AirPods",
    0x0F20: "AirPods (2nd generation)",
    0x1320: "AirPods (3rd generation)",
    0x1920: "AirPods (4th generation)",
    0x0E20: "AirPods Pro",
    0x1420: "AirPods Pro (2nd generation)",
    0x2420: "AirPods Pro (2nd generation)",
    0x0A20: "AirPods Max",
}


@dataclass
class PodsInfo:
    model: str
    left: int | None       # percent, None = not in ear/case
    right: int | None
    case: int | None
    left_charging: bool
    right_charging: bool
    case_charging: bool


def parse_airpods(data: bytes) -> PodsInfo | None:
    """Parse Apple proximity-pairing payload (type 0x07). Best-effort, community-documented."""
    if len(data) < 9 or data[0] != 0x07:
        return None
    model_id = (data[3] << 8) | data[4]
    status = data[5]
    flipped = (status & 0x20) == 0
    pods = data[6]
    flags = data[7] >> 4
    case_nib = data[7] & 0x0F

    def pct(n: int) -> int | None:
        return None if n > 10 else n * 10

    hi, lo = pods >> 4, pods & 0x0F
    left_n, right_n = (lo, hi) if flipped else (hi, lo)
    left_chg = bool(flags & (0x01 if flipped else 0x02))
    right_chg = bool(flags & (0x02 if flipped else 0x01))
    return PodsInfo(
        model=MODELS.get(model_id, "AirPods"),
        left=pct(left_n), right=pct(right_n), case=pct(case_nib),
        left_charging=left_chg, right_charging=right_chg,
        case_charging=bool(flags & 0x04),
    )


class Scanner(QObject):
    found = Signal(object)
    error = Signal(str)

    def start(self):
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            asyncio.run(self._run())
        except Exception as e:  # e.g. Bluetooth off / no adapter
            self.error.emit(f"Bluetooth scan stopped: {e}")

    async def _run(self):
        from bleak import BleakScanner

        def cb(device, adv):
            data = adv.manufacturer_data.get(APPLE_COMPANY_ID)
            if not data or adv.rssi < RSSI_THRESHOLD:
                return
            info = parse_airpods(bytes(data))
            if info:
                self.found.emit(info)

        async with BleakScanner(cb):
            while True:
                await asyncio.sleep(1)


class Popup(QWidget):
    W, H = 380, 330

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(self.W, self.H)
        self.info: PodsInfo | None = None
        self.last_hidden = 0.0

        self.connect_btn = QPushButton("Connect", self)
        self.connect_btn.setGeometry(40, 262, 300, 44)
        self.connect_btn.setCursor(Qt.PointingHandCursor)
        self.connect_btn.setStyleSheet(
            "QPushButton{background:#0A84FF;color:white;border:none;border-radius:22px;"
            "font:600 15px 'Segoe UI';}QPushButton:hover{background:#2D95FF;}"
            "QPushButton:pressed{background:#0670DD;}")
        self.connect_btn.clicked.connect(self.on_connect)

        self.close_btn = QPushButton("✕", self)
        self.close_btn.setGeometry(self.W - 44, 12, 30, 30)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.12);color:#ddd;border:none;"
            "border-radius:15px;font:600 12px 'Segoe UI';}"
            "QPushButton:hover{background:rgba(255,255,255,0.22);}")
        self.close_btn.clicked.connect(self.dismiss)

        self.timer = QTimer(self, singleShot=True, interval=AUTO_HIDE_MS)
        self.timer.timeout.connect(self.dismiss)
        self.anim = QPropertyAnimation(self, b"pos", self)
        self.anim.setDuration(380)

    # ---- behaviour -------------------------------------------------------
    def show_info(self, info: PodsInfo):
        if self.isVisible():
            self.info = info
            self.update()
            return
        if time.time() - self.last_hidden < COOLDOWN_SECONDS:
            return
        self.info = info
        self.connect_btn.setText("Connect")
        geo = QGuiApplication.primaryScreen().availableGeometry()
        x = geo.x() + (geo.width() - self.W) // 2
        y_end = geo.y() + geo.height() - self.H - 24
        self.move(x, geo.y() + geo.height())
        self.show()
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(self.pos().__class__(x, y_end))
        try:
            self.anim.finished.disconnect()
        except RuntimeError:
            pass
        self.anim.start()
        self.timer.start()

    def dismiss(self):
        self.timer.stop()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.anim.setEasingCurve(QEasingCurve.InCubic)
        self.anim.setStartValue(self.pos())
        self.anim.setEndValue(self.pos().__class__(self.x(), geo.y() + geo.height()))
        self.anim.finished.connect(self._after_hide)
        self.anim.start()

    def _after_hide(self):
        self.anim.finished.disconnect(self._after_hide)
        self.hide()
        self.last_hidden = time.time()

    def on_connect(self):
        # Opens Windows Bluetooth settings where the AirPods can be paired/connected.
        QDesktopServices.openUrl(QUrl("ms-settings:bluetooth"))
        self.connect_btn.setText("Opening Bluetooth settings…")
        QTimer.singleShot(1800, self.dismiss)

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        card = QRectF(8, 8, self.W - 16, self.H - 16)
        path = QPainterPath()
        path.addRoundedRect(card, 28, 28)
        p.fillPath(path, QColor(28, 28, 32, 245))
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.drawPath(path)

        info = self.info
        if not info:
            return

        # earbuds + case illustration
        def pill(cx, cy, w, h, color):
            r = QPainterPath()
            r.addRoundedRect(QRectF(cx - w / 2, cy - h / 2, w, h), w / 2, w / 2)
            p.fillPath(r, color)

        white = QColor(240, 240, 243)
        p.setPen(Qt.NoPen)
        pill(self.W / 2 - 72, 76, 30, 62, white)       # left bud
        pill(self.W / 2 + 72, 76, 30, 62, white)       # right bud
        case = QPainterPath()
        case.addRoundedRect(QRectF(self.W / 2 - 52, 58, 104, 64), 22, 22)
        p.fillPath(case, QColor(232, 232, 236))
        p.setPen(QPen(QColor(200, 200, 205), 2))
        p.drawLine(int(self.W / 2 - 52), 84, int(self.W / 2 + 52), 84)

        # title
        p.setPen(QColor("white"))
        f = QFont("Segoe UI", 15, QFont.DemiBold)
        p.setFont(f)
        p.drawText(QRectF(0, 140, self.W, 28), Qt.AlignCenter, info.model)

        # battery columns
        cols = [("L", info.left, info.left_charging),
                ("Case", info.case, info.case_charging),
                ("R", info.right, info.right_charging)]
        cw = (self.W - 80) / 3
        for i, (label, val, chg) in enumerate(cols):
            cx = 40 + cw * i + cw / 2
            self._ring(p, cx, 205, 24, val, chg)
            p.setPen(QColor(160, 160, 168))
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(QRectF(cx - 40, 232, 80, 18), Qt.AlignCenter, label)

    def _ring(self, p: QPainter, cx, cy, r, val, charging):
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 35), 5))
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
        if val is not None:
            color = QColor("#30D158") if (val > 20 or charging) else QColor("#FF453A")
            p.setPen(QPen(color, 5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), 90 * 16, int(-val / 100 * 360 * 16))
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", 10, QFont.DemiBold))
        txt = "—" if val is None else f"{val}%"
        p.drawText(QRectF(cx - r, cy - r, 2 * r, 2 * r), Qt.AlignCenter, txt)
        if charging:
            p.setPen(QColor("#30D158"))
            p.setFont(QFont("Segoe UI", 9))
            p.drawText(QRectF(cx - r, cy - r - 18, 2 * r, 16), Qt.AlignCenter, "⚡")


DEMO_INFO = PodsInfo("AirPods Pro (2nd generation)", 90, 80, 60, False, False, True)


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0A84FF"))
    p.drawEllipse(2, 2, 60, 60)
    p.setBrush(QColor("white"))
    p.drawRoundedRect(18, 14, 12, 28, 6, 6)
    p.drawRoundedRect(34, 14, 12, 28, 6, 6)
    p.drawRoundedRect(22, 40, 20, 10, 5, 5)
    p.end()
    return QIcon(pm)


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pyw}" "{__file__}"'


def startup_enabled() -> bool:
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled: bool):
    if not winreg:
        return
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    popup = Popup()

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip("AirPods Popup — watching for AirPods")
    menu = QMenu()
    test = QAction("Show test popup", menu)
    test.triggered.connect(lambda: (setattr(popup, "last_hidden", 0.0),
                                    popup.show_info(DEMO_INFO)))
    menu.addAction(test)
    startup = QAction("Run at Windows startup", menu, checkable=True)
    startup.setChecked(startup_enabled())
    startup.toggled.connect(set_startup)
    menu.addAction(startup)
    menu.addSeparator()
    quit_a = QAction("Quit", menu)
    quit_a.triggered.connect(app.quit)
    menu.addAction(quit_a)
    tray.setContextMenu(menu)
    tray.show()

    if "--demo" in sys.argv:
        QTimer.singleShot(300, lambda: popup.show_info(DEMO_INFO))
    else:
        scanner = Scanner()
        scanner.found.connect(popup.show_info)
        scanner.error.connect(lambda m: tray.showMessage("AirPods Popup", m,
                                                         QSystemTrayIcon.Warning, 6000))
        scanner.start()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
