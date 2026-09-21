"""Native, asynchronous desktop interface for the photo importer."""

from collections import Counter
from datetime import date
from pathlib import Path
import sys
import threading
import time

from PySide6.QtCore import QAbstractTableModel, QDate, QModelIndex, QSettings, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from .core import ImportOptions, discover_sources, import_media, scan_media


def human_size(value):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,} B"
        value /= 1024


class Task(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel = threading.Event()
        self.last_update = 0.0

    def report(self, *values):
        now = time.monotonic()
        if now - self.last_update >= 0.08:
            self.last_update = now
            self.progress.emit(values)

    def run(self):
        try:
            self.result.emit(self.operation(self.cancel, self.report))
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class PreviewModel(QAbstractTableModel):
    headers = ("Filename", "Type", "Date", "Size", "Destination")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []

    def replace(self, items):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{item.source}\nDate: {item.date_source}\n{item.relative_destination}"
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor("#8dddd1")
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                item.source.name, item.source.suffix.lstrip(".").upper(),
                item.taken_at.strftime("%d %b %Y"), human_size(item.size),
                str(item.relative_destination),
            )[index.column()]
        return None


STYLE = """
QWidget { background: #17191e; color: #e9edf3; font-size: 13px; }
QMainWindow { background: #17191e; }
QLabel#Title { font-size: 27px; font-weight: 650; }
QLabel#Section { font-size: 15px; font-weight: 650; }
QLabel#Muted { color: #9fa7b6; }
QLabel#Metric { font-size: 24px; font-weight: 600; color: #c4f3ec; }
QLabel#Status { color: #c4f3ec; font-size: 14px; }
QFrame#Card { background: #202329; border: 1px solid #353a44; border-radius: 9px; }
QFrame#Card QLabel, QFrame#Card QWidget { background: transparent; }
QLineEdit, QComboBox, QDateEdit { background: #111318; border: 1px solid #414855;
    border-radius: 5px; padding: 8px; min-height: 19px; selection-background-color: #387a70; }
QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 1px solid #7dd6c7; }
QComboBox QAbstractItemView { background: #252a32; selection-background-color: #354a49; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 22px; }
QPushButton { background: #303640; border: 1px solid #454e5c; border-radius: 5px;
    padding: 9px 14px; font-weight: 550; min-height: 18px; }
QPushButton:hover { background: #414955; }
QPushButton:pressed { background: #252b33; }
QPushButton#Primary { background: #a1e4d7; border: 1px solid #a1e4d7; color: #142921; }
QPushButton#Primary:hover { background: #c4f3e9; }
QPushButton:disabled, QPushButton#Primary:disabled { background: #252a31; border-color: #353a44; color: #747f8c; }
QWidget:disabled { color: #747f8c; }
QCheckBox { spacing: 9px; padding: 5px 0; }
QCheckBox::indicator { width: 16px; height: 16px; }
QTableView { background: #1b1e24; alternate-background-color: #20242b;
    border: 1px solid #353a44; border-radius: 6px; gridline-color: #292e37;
    selection-background-color: #354a49; }
QHeaderView::section { background: #272c34; color: #adb8c7; border: none;
    border-bottom: 1px solid #414855; padding: 10px 8px; text-align: left; }
QProgressBar { background: #303640; border: none; border-radius: 3px; min-height: 7px; max-height: 7px; }
QProgressBar::chunk { background: #a1e4d7; border-radius: 3px; }
QPlainTextEdit { background: #111318; border: 1px solid #414855; border-radius: 5px; padding: 8px; }
QSplitter::handle { background: #17191e; }
QScrollArea { border: none; }
QToolTip { background: #303640; color: #e9edf3; border: 1px solid #454e5c; padding: 6px; }
"""


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(wrap)
    if name:
        widget.setObjectName(name)
    return widget


class ImportWindow(QMainWindow):
    def __init__(self, settings=None, auto_detect=True):
        super().__init__()
        self.settings = settings if settings is not None else QSettings("CameraTools", "PhotoImporter")
        self.scan = None
        self.task = None
        self.detector = None
        self.closing = False
        self.devices = []
        self.auto_selected = None
        self.started_at = 0
        self.phase = None
        self.setWindowTitle("Photo Importer")
        self.resize(1220, 850)
        self.setMinimumSize(960, 720)
        container = QWidget()
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(18)
        header = QHBoxLayout()
        title_column = QVBoxLayout()
        title_column.addWidget(label("Photo Importer", "Title"))
        title_column.addWidget(label("Import into your Lightroom photo library.", "Muted"))
        header.addLayout(title_column)
        header.addStretch()
        header.addWidget(label("YYYY / MM / DD", "Muted"))
        layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(24)
        layout.addWidget(splitter, 1)
        self.controls = QWidget()
        left = QVBoxLayout(self.controls)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(10)
        self.controls.setMinimumWidth(315)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.controls)
        scroll.setMinimumWidth(345)
        sidebar = QWidget()
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(12)
        sidebar_layout.addWidget(scroll, 1)
        splitter.addWidget(sidebar)
        left.addWidget(label("1   Source", "Section"))
        self.source = QComboBox()
        self.source.setEditable(True)
        self.source.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.source.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.source.lineEdit().setPlaceholderText("Connect a camera or SD card…")
        self.source.setAccessibleName("Source folder")
        left.addWidget(self.source)
        source_buttons = QHBoxLayout()
        browse = QPushButton("Choose folder…")
        browse.clicked.connect(self.choose_source)
        self.refresh = QPushButton("Refresh")
        self.refresh.clicked.connect(self.detect_devices)
        source_buttons.addWidget(browse, 1)
        source_buttons.addWidget(self.refresh)
        left.addLayout(source_buttons)
        self.device_hint = label("Looking for mounted cameras and cards…", "Muted", True)
        left.addWidget(self.device_hint)

        left.addSpacing(5)
        left.addWidget(label("2   Destination", "Section"))
        self.destination = QLineEdit(str(self.settings.value("destination", str(Path.home() / "Pictures"))))
        self.destination.setAccessibleName("Destination folder")
        left.addWidget(self.destination)
        destination_button = QPushButton("Choose destination…")
        destination_button.clicked.connect(self.choose_destination)
        left.addWidget(destination_button)
        left.addWidget(label("yyyy / mm / dd\nJPEG + RAW + sidecars stay together.", "Muted", True))

        left.addSpacing(5)
        left.addWidget(label("3   Import options", "Section"))
        self.use_cutoff = QCheckBox("Only on or after")
        self.use_cutoff.setChecked(self.settings.value("use_cutoff", False, type=bool))
        self.cutoff = QDateEdit()
        self.cutoff.setCalendarPopup(True)
        self.cutoff.setDisplayFormat("dd MMM yyyy")
        saved_date = QDate.fromString(str(self.settings.value("cutoff", "")), "yyyy-MM-dd")
        self.cutoff.setDate(saved_date if saved_date.isValid() else QDate.currentDate().addDays(-30))
        self.cutoff.setEnabled(self.use_cutoff.isChecked())
        self.cutoff.setAccessibleName("Earliest date to import, inclusive")
        cutoff_row = QHBoxLayout()
        cutoff_row.addWidget(self.use_cutoff)
        cutoff_row.addWidget(self.cutoff, 1)
        left.addLayout(cutoff_row)
        self.basis = QComboBox()
        self.basis.addItem("Date taken (EXIF when available)", "capture")
        self.basis.addItem("File modified date (original behavior)", "modified")
        self.basis.setCurrentIndex(max(0, self.basis.findData(self.settings.value("date_basis", "capture"))))
        self.basis.setAccessibleName("Date used for cutoff and folders")
        left.addWidget(self.basis)
        self.videos = QCheckBox("Include videos")
        self.videos.setChecked(self.settings.value("videos", True, type=bool))
        left.addWidget(self.videos)
        left.addWidget(label("Matching files are checked before skipping. Different photos with the same name are kept.", "Muted", True))
        left.addStretch()
        self.preview_button = QPushButton("Preview import")
        self.preview_button.clicked.connect(self.start_scan)
        sidebar_layout.addWidget(self.preview_button)

        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        splitter.addWidget(right_widget)
        splitter.setSizes([355, 785])
        summary = QFrame()
        summary.setObjectName("Card")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(20, 16, 20, 16)
        self.count = label("—", "Metric")
        self.size_metric = label("—", "Metric")
        self.days = label("—", "Metric")
        for metric, caption in ((self.count, "files in preview"), (self.size_metric, "to check / copy"), (self.days, "date folders")):
            column = QVBoxLayout()
            column.addWidget(metric)
            column.addWidget(label(caption, "Muted"))
            summary_layout.addLayout(column, 1)
        right.addWidget(summary)
        self.preview_title = label("Your import preview", "Section")
        right.addWidget(self.preview_title)
        self.empty = label("Connect your card or choose a folder.\nPreview the files before importing.", "Muted", True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(100)
        right.addWidget(self.empty, 1)
        self.model = PreviewModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((160, 62, 104, 85)):
            self.table.setColumnWidth(column, width)
        right.addWidget(self.table, 1)
        self.table.hide()
        self.preview_note = label("No files have been copied.", "Muted", True)
        right.addWidget(self.preview_note)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(95)
        self.details.hide()
        right.addWidget(self.details)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        footer = QHBoxLayout()
        status_column = QVBoxLayout()
        self.status = label("Ready when you are", "Status", True)
        self.activity = label("Originals always stay on the card.", "Muted", True)
        status_column.addWidget(self.status)
        status_column.addWidget(self.activity)
        footer.addLayout(status_column, 1)
        self.open_folder = QPushButton("Open destination")
        self.open_folder.clicked.connect(self.reveal_destination)
        self.open_folder.hide()
        footer.addWidget(self.open_folder)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        self.import_button = QPushButton("Import files")
        self.import_button.setObjectName("Primary")
        self.import_button.setMinimumWidth(170)
        self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.start_import)
        footer.addWidget(self.import_button)
        layout.addLayout(footer)

        self.source.currentTextChanged.connect(self.invalidate)
        self.destination.textChanged.connect(self.invalidate)
        self.use_cutoff.toggled.connect(self.cutoff.setEnabled)
        self.use_cutoff.toggled.connect(self.invalidate)
        self.cutoff.dateChanged.connect(self.invalidate)
        self.basis.currentIndexChanged.connect(self.invalidate)
        self.videos.toggled.connect(self.invalidate)
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.detect_devices)
        if auto_detect:
            self.timer.start()
            QTimer.singleShot(0, self.detect_devices)

    def choose_source(self):
        path = QFileDialog.getExistingDirectory(self, "Choose camera or source folder", self.source.currentText() or str(Path.home()))
        if path:
            self.auto_selected = None
            self.source.setEditText(path)

    def choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "Choose photo library", self.destination.text())
        if path:
            self.destination.setText(path)

    def invalidate(self, *_):
        if self.task:
            return
        self.scan = None
        self.model.replace([])
        self.table.hide()
        self.empty.show()
        self.empty.setText("Preview the files with these settings before importing.")
        self.count.setText("—")
        self.size_metric.setText("—")
        self.days.setText("—")
        self.import_button.setEnabled(False)
        self.import_button.setText("Import files")
        self.preview_note.setText("No files have been copied with these settings.")
        self.details.hide()
        self.open_folder.hide()
        self.progress.setValue(0)
        self.status.setText("Ready to preview")
        self.activity.setText("Originals always stay on the card.")

    def detect_devices(self):
        if self.task or self.detector or self.closing:
            return
        self.detector = Task(lambda *_: discover_sources(), self)
        self.detector.result.connect(self.devices_found)
        self.detector.failed.connect(lambda error: self.device_hint.setText(f"Detection unavailable: {error}. Choose a folder manually."))
        self.detector.finished.connect(self.detection_finished)
        self.detector.start()

    def detection_finished(self):
        self.detector.deleteLater()
        self.detector = None
        self.finish_close()

    def devices_found(self, devices):
        if self.task or self.closing:
            return
        paths = [str(device.path) for device in devices]
        old_text = self.source.currentText()
        if old_text == self.auto_selected and old_text not in paths:
            old_text = ""
            self.auto_selected = None
        if not old_text and len(paths) == 1:
            old_text = paths[0]
            self.auto_selected = old_text
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItems(paths)
        self.source.setEditText(old_text)
        self.source.blockSignals(False)
        if paths != self.devices:
            self.devices = paths
            self.invalidate()
        if len(devices) == 1:
            self.device_hint.setText(f"Detected: {devices[0].label}")
        elif devices:
            self.device_hint.setText(f"{len(devices)} cards detected. Choose one above.")
        else:
            self.device_hint.setText("No mounted camera or card. Use a card reader, USB mass-storage mode, or choose a folder.")

    def options(self):
        if not self.source.currentText().strip():
            raise ValueError("Connect a card or choose a source folder first.")
        if not self.destination.text().strip():
            raise ValueError("Choose a destination folder first.")
        return ImportOptions(
            source=Path(self.source.currentText().strip()).expanduser(),
            destination=Path(self.destination.text().strip()).expanduser(),
            cutoff=date.fromisoformat(self.cutoff.date().toString("yyyy-MM-dd")) if self.use_cutoff.isChecked() else None,
            include_videos=self.videos.isChecked(), date_basis=self.basis.currentData(),
        )

    def save_settings(self):
        for key, value in {
            "destination": self.destination.text(), "use_cutoff": self.use_cutoff.isChecked(),
            "cutoff": self.cutoff.date().toString("yyyy-MM-dd"), "date_basis": self.basis.currentData(),
            "videos": self.videos.isChecked(),
        }.items():
            self.settings.setValue(key, value)

    def start_scan(self):
        try:
            options = self.options()
        except ValueError as error:
            self.show_error(str(error))
            return
        self.invalidate()
        self.save_settings()
        self.status.setText("Reading your card…")
        self.activity.setText("Finding photos and matching RAW / JPEG pairs.")
        self.begin_task("scan", lambda cancel, report: scan_media(options, cancel=cancel, progress=report), self.scanned)

    def begin_task(self, phase, operation, on_result):
        self.phase = phase
        self.started_at = time.monotonic()
        self.controls.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.import_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.cancel_button.show()
        self.details.hide()
        self.progress.setRange(0, 0 if phase == "scan" else 1000)
        self.progress.setValue(0)
        self.task = Task(operation, self)
        self.task.result.connect(on_result)
        self.task.failed.connect(self.show_error)
        self.task.progress.connect(self.task_progress)
        self.task.finished.connect(self.task_finished)
        self.task.start()

    def scanned(self, scan):
        if scan.cancelled:
            self.status.setText("Preview cancelled")
            self.activity.setText("No files copied. Preview again when ready.")
            return
        self.scan = scan
        self.model.replace(scan.items)
        self.table.setVisible(bool(scan.items))
        self.empty.setVisible(not scan.items)
        self.empty.setText("No matching photos or videos.\nCheck your source and cutoff date.")
        self.count.setText(f"{len(scan.items):,}")
        self.size_metric.setText(human_size(scan.total_bytes))
        self.days.setText(str(len({item.relative_destination.parent for item in scan.items})))
        counts = Counter(item.kind for item in scan.items)
        description = " · ".join(f"{count:,} {kind}" for kind, count in sorted(counts.items()))
        notes = [description] if description else []
        if scan.filtered:
            notes.append(f"{scan.filtered:,} excluded by filters")
        if scan.fallback_count:
            notes.append(f"{scan.fallback_count:,} files use modified dates (capture date unavailable)")
        notes.append("Identical files are skipped during import; name conflicts receive matching suffixes.")
        self.preview_note.setText("\n".join(notes))
        if scan.warnings:
            self.details.setPlainText("\n".join(scan.warnings))
            self.details.show()
        self.status.setText("Preview ready" if scan.items else "Nothing to import")
        self.activity.setText("Review the dates and destinations, then import." if scan.items else "Try a different folder or earlier date.")
        self.import_button.setText(f"Import {len(scan.items):,} files")

    def start_import(self):
        if not self.scan or self.task:
            return
        scan = self.scan
        self.status.setText("Importing your photos…")
        self.activity.setText("Checking existing files and copying safely.")
        self.begin_task("import", lambda cancel, report: import_media(scan, cancel=cancel, progress=report), self.imported)

    def imported(self, result):
        title = "Import cancelled" if result.cancelled else ("Import finished with errors" if result.errors else "Import complete")
        self.status.setText(title)
        self.activity.setText(f"{result.copied:,} copied · {result.skipped:,} already present · {result.renamed:,} renamed · {len(result.errors):,} errors")
        self.preview_note.setText("Completed files are in your destination. In Lightroom Classic, use Import → Add and choose that folder.")
        if result.errors:
            self.details.setPlainText("\n".join(result.errors))
            self.details.show()
        self.open_folder.setVisible(Path(self.destination.text()).expanduser().is_dir())
        self.scan = None

    def task_progress(self, values):
        if self.phase == "scan":
            count, name = values
            self.status.setText(f"Scanning · {count:,} files found")
            self.activity.setText(str(name)[-110:])
        else:
            done, total, name = values
            self.progress.setValue(int(done * 1000 / total) if total else 0)
            elapsed = max(time.monotonic() - self.started_at, 0.1)
            self.status.setText(f"Importing · {human_size(done)} / {human_size(total)}")
            self.activity.setText(f"{human_size(done / elapsed)}/s processed · {str(name)[-85:]}")

    def task_finished(self):
        self.task.deleteLater()
        self.task = None
        self.controls.setEnabled(True)
        self.preview_button.setEnabled(True)
        self.cancel_button.hide()
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000 if self.status.text() == "Import complete" else 0)
        self.import_button.setEnabled(bool(self.scan and self.scan.items))
        self.finish_close()

    def cancel_task(self):
        if self.task:
            self.task.cancel.set()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("Cancelling…")
            self.status.setText("Stopping safely…")

    def show_error(self, message):
        self.scan = None
        self.import_button.setEnabled(False)
        self.status.setText("Could not finish")
        self.activity.setText(message)
        self.details.setPlainText(message)
        self.details.show()

    def reveal_destination(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.destination.text()).expanduser().resolve())))

    def finish_close(self):
        if self.closing and not self.task and not self.detector:
            self.close()

    def closeEvent(self, event):
        self.timer.stop()
        if self.task or self.detector:
            self.closing = True
            self.cancel_task()
            event.ignore()
        else:
            event.accept()


def run():
    app = QApplication(sys.argv)
    app.setApplicationName("Photo Importer")
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#17191e"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#111318"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e9edf3"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e9edf3"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#303640"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e9edf3"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#387a70"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setFont(QFont(".AppleSystemUIFont" if sys.platform == "darwin" else "Sans Serif", 11))
    app.setStyleSheet(STYLE)
    window = ImportWindow()
    window.show()
    return app.exec()
