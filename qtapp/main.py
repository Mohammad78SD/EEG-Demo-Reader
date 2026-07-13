import sys

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from reader import FileReader  # noqa: E402

from sweepbuffer import SweepBuffer
from worker import ProducerWorker

SAMPLE_INTERVAL_MS = 2  # 500Hz
WINDOW = 2500  # 90s sweep window, matches the web prototype
REDRAW_MS = 33  # ~30fps redraw cap, independent of the 500Hz ingest rate
DEFAULT_VISIBLE = 3  # channels checked on first launch, before any settings exist
ORG, APP = "negand", "eeg-dashboard"  # QSettings location key

# CLAUDE.md flags this as a Pi-only empirical call — the software GL driver
# on the Pi 3B/4B can be slower or less stable than Qt's CPU rasterizer.
# Leave False on dev machines; flip and re-run the benchmark on the Pi.
USE_OPENGL = False

# CLAUDE.md perf checklist: antialiasing costs real fps at 500Hz ingest —
# global option, must be set before any PlotWidget/curve is constructed.
pg.setConfigOptions(antialias=False, useOpenGL=USE_OPENGL)


def distinct_color(i: int, n: int) -> QColor:
    # Fixed saturation/lightness, spread hue — darker (100/255 lightness)
    # for readability against the white plot background.
    hue = round(i * 360 / n) if n else 0
    return QColor.fromHsl(hue, 200, 100)


class MainWindow(QMainWindow):
    def __init__(self, reader: FileReader):
        super().__init__()
        self.setWindowTitle("EEG Dashboard (Qt)")
        self.resize(1100, 700)

        self.reader = reader
        self.settings = QSettings(ORG, APP)
        self.sweep = SweepBuffer(reader.num_channels, WINDOW, SAMPLE_INTERVAL_MS)
        self.dirty = False
        self.msg_count = 0
        self.sample_count = 0

        central = QWidget()
        self.setCentralWidget(central)
        outer = QHBoxLayout(central)

        # Left column: scrollable checkbox list. QScrollArea wraps a plain
        # QWidget/QVBoxLayout — the scroll area only handles clipping and
        # the scrollbar, layout logic is unchanged from a normal container.
        picker_container = QWidget()
        picker_layout = QVBoxLayout(picker_container)
        scroll = QScrollArea()
        scroll.setWidget(picker_container)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(150)
        outer.addWidget(scroll)

        # Right column: metrics/replay strip on top, plot fills the rest.
        right = QVBoxLayout()
        outer.addLayout(right)

        top_strip = QHBoxLayout()
        self.metrics_label = QLabel("connecting...")
        self.replay_button = QPushButton("Replay")
        self.replay_button.clicked.connect(self._on_replay)
        top_strip.addWidget(self.metrics_label)
        top_strip.addStretch()
        top_strip.addWidget(self.replay_button)
        right.addLayout(top_strip)

        plot = pg.PlotWidget()
        right.addWidget(plot)
        plot.setBackground("w")
        plot.setYRange(-60, 60)
        plot.setXRange(0, float(self.sweep.xs[-1]))
        plot.setLabel("bottom", "Time (ms)")
        # Ranges are pinned by design (fixed sweep window, fixed y scale) —
        # an operator dashboard shouldn't let a stray drag/scroll/click
        # zoom or pan it away from that, so all interactive range changes
        # are switched off at the ViewBox level.
        plot.setMouseEnabled(x=False, y=False)
        plot.setMenuEnabled(False)
        plot.hideButtons()  # the corner "A" auto-range button

        self.curves = []
        for i, name in enumerate(reader.channels):
            default_visible = i < DEFAULT_VISIBLE
            visible = self.settings.value(f"visible/{name}", default_visible, type=bool)
            color = distinct_color(i, len(reader.channels))

            curve = plot.plot(pen=pg.mkPen(color=color, width=1))
            # Auto-decimates points beyond pixel resolution ('peak' keeps
            # local min/max per pixel column, so spikes survive) and skips
            # drawing data outside the current view — both are no-ops here
            # since the view never pans, but cheap insurance regardless.
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
            curve.setVisible(visible)
            self.curves.append(curve)

            checkbox = QCheckBox(name)
            checkbox.setChecked(visible)
            checkbox.setStyleSheet(f"color: {color.name()}")
            # Default arguments capture i/name by value at connect time —
            # without them every callback would see the loop's final i/name.
            checkbox.stateChanged.connect(
                lambda state, i=i, name=name: self._on_visibility_changed(i, name, state)
            )
            picker_layout.addWidget(checkbox)
        picker_layout.addStretch()

        self._start_worker()

        self.redraw_timer = QTimer(self)
        self.redraw_timer.timeout.connect(self._redraw)
        self.redraw_timer.start(REDRAW_MS)

        self.metrics_timer = QTimer(self)
        self.metrics_timer.timeout.connect(self._update_metrics)
        self.metrics_timer.start(1000)

    def _start_worker(self):
        self.worker = ProducerWorker(self.reader)
        self.worker.batch_ready.connect(self._on_batch)
        self.worker.finished_playback.connect(self._on_finished)
        self.worker.start()

    def _on_visibility_changed(self, index: int, name: str, state: int):
        visible = bool(state)
        self.curves[index].setVisible(visible)
        self.settings.setValue(f"visible/{name}", visible)

    def _on_batch(self, batch: np.ndarray):
        self.sweep.push(batch)
        self.msg_count += 1
        self.sample_count += batch.shape[0]
        self.dirty = True

    def _on_finished(self):
        self.setWindowTitle("EEG Dashboard (Qt) — playback complete")

    def _redraw(self):
        if not self.dirty:
            return
        for ch, curve in enumerate(self.curves):
            if curve.isVisible():  # skip data churn for channels nobody sees
                # connect="finite" breaks the line at NaN — the unwritten
                # tail of the sweep window — instead of drawing a stray
                # segment back to the last real sample.
                curve.setData(self.sweep.xs, self.sweep.data[ch], connect="finite")
        self.dirty = False

    def _update_metrics(self):
        self.metrics_label.setText(
            f"{self.msg_count} msg/s | {self.sample_count} samples/s"
        )
        self.msg_count = 0
        self.sample_count = 0

    def _on_replay(self):
        self.replay_button.setEnabled(False)
        self.worker.stop()  # blocks briefly until the producer thread exits
        self.sweep.reset()
        for curve in self.curves:
            curve.setData([], [])
        self.dirty = False
        self.setWindowTitle("EEG Dashboard (Qt)")
        self._start_worker()
        self.replay_button.setEnabled(True)

    def closeEvent(self, event):
        self.worker.stop()
        super().closeEvent(event)


def main():
    reader = FileReader()

    app = QApplication(sys.argv)
    window = MainWindow(reader)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
