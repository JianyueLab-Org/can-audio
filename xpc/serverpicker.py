"""异步服务器选择控件；刷新不会改动已保存的端点。"""
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, LineEdit, PushButton, SpinBox

import serverdirectory
from i18n import t


class _Result(QObject):
    ready = pyqtSignal(object)


class ServerPicker(QWidget):
    def __init__(self, kind, host, port, parent=None, auto_refresh=True):
        super().__init__(parent)
        self.kind = kind
        self._locked = False
        self._busy = False
        self._error = False
        self._custom = (host, int(port))
        self._servers = []
        self._index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(BodyLabel(t(f"server.{kind}")))
        choices = QHBoxLayout()
        self.server_combo = ComboBox()
        self.server_combo.addItem(t("server.custom"))
        self.refresh_button = PushButton(t("server.refresh"))
        choices.addWidget(self.server_combo, 1)
        choices.addWidget(self.refresh_button)
        layout.addLayout(choices)
        endpoint = QHBoxLayout()
        self.host_edit = LineEdit()
        self.host_edit.setText(host)
        self.host_edit.setPlaceholderText(t("server.host"))
        self.port_edit = SpinBox()
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(int(port))
        endpoint.addWidget(self.host_edit, 1)
        endpoint.addWidget(self.port_edit)
        layout.addLayout(endpoint)
        self.status_label = CaptionLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self._result = _Result(self)
        self._result.ready.connect(self._loaded)
        self.server_combo.currentIndexChanged.connect(self._selected)
        self.host_edit.textEdited.connect(self._edited)
        self.port_edit.valueChanged.connect(self._edited)
        self.refresh_button.clicked.connect(self.refresh)
        if auto_refresh:
            self.refresh()

    def host(self):
        return self.host_edit.text().strip()

    def port(self):
        return self.port_edit.value()

    def set_locked(self, locked):
        self._locked = bool(locked)
        self._controls()

    def _controls(self):
        self.server_combo.setEnabled(not self._locked)
        self.host_edit.setEnabled(not self._locked)
        self.port_edit.setEnabled(not self._locked)
        self.refresh_button.setEnabled(not self._locked and not self._busy)
        key = ("server.locked" if self._locked else "server.loading" if self._busy
               else "server.failed" if self._error else "server.empty" if not self._servers
               else "server.ready")
        self.status_label.setText(t(key))

    def _edited(self, *_):
        if self._locked:
            return
        self._custom = (self.host(), self.port())
        self._index = 0
        self.server_combo.blockSignals(True)
        self.server_combo.setCurrentIndex(0)
        self.server_combo.blockSignals(False)

    def _selected(self, index):
        if self._locked:
            self.server_combo.blockSignals(True)
            self.server_combo.setCurrentIndex(self._index)
            self.server_combo.blockSignals(False)
            return
        if self._index == 0:
            self._custom = (self.host(), self.port())
        if index == 0:
            host, port = self._custom
        elif 0 < index <= len(self._servers):
            server = self._servers[index - 1]
            host, port = server.host, server.port
        else:
            return
        self._index = index
        self.host_edit.setText(host)
        self.port_edit.blockSignals(True)
        self.port_edit.setValue(port)
        self.port_edit.blockSignals(False)

    def refresh(self):
        if self._busy or self._locked:
            return
        self._busy = True
        self._controls()
        bridge = self._result

        def work():
            result = serverdirectory.fetch()
            try:
                bridge.ready.emit(result)
            except RuntimeError:
                # 对话框已关闭时丢弃迟到的回包。
                pass

        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, result):
        self._busy = False
        self._error = result is None
        if result is not None:
            current = (self.host(), self.port())
            self._servers = result.get(self.kind, [])
            self.server_combo.blockSignals(True)
            self.server_combo.clear()
            self.server_combo.addItem(t("server.custom"))
            selected = 0
            for index, server in enumerate(self._servers, 1):
                self.server_combo.addItem(server.label)
                if (server.host, server.port) == current:
                    selected = index
            self._index = selected
            self.server_combo.setCurrentIndex(selected)
            self.server_combo.blockSignals(False)
        self._controls()
