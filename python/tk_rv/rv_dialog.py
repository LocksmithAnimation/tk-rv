from sgtk.platform.qt import QtGui, QtCore


class RvDialog(QtGui.QDialog):
    dialog_closed = QtCore.Signal(object)

    def __init__(self, parent=None, f=QtCore.Qt.WindowFlags()):
        super(RvDialog, self).__init__(parent, f)
        self._widget = None
        self.layout = QtGui.QVBoxLayout(self)

    @property
    def widget(self):
        return self._widget

    @widget.setter
    def widget(self, value):
        self._widget = value
        self._widget.setParent(self)
        self.layout.addWidget(self._widget)
        self.resize(self._widget.width(), self._widget.height())

    def done(self, exit_code):
        if self._widget:
            if self._widget.close():
                pass
            else:
                return
        else:
            self._do_done(exit_code)

    def _do_done(self, exit_code):
        super(RvDialog, self).done(exit_code)

        self.dialog_closed.emit(self)
