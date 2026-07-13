import threading

import numpy as np
from PySide6.QtCore import QThread, Signal

from reader import FileReader

BATCH_SIZE = 16  # ~32ms of samples per signal emission, not one signal per 2ms sample


class ProducerWorker(QThread):
    """Runs FileReader.run() on a background thread at the true 2ms cadence.

    Qt's rule: never touch widgets from a worker thread. Signals are the
    escape hatch — emitting one here doesn't call the connected slot
    directly. Because the receiver (MainWindow) lives on the main thread,
    Qt auto-detects the cross-thread case and queues the call: it gets
    dispatched on the main thread's event loop, at whatever point that
    loop is free to process it. That queuing is what makes this safe.
    """

    batch_ready = Signal(np.ndarray)  # shape (n, num_channels)
    finished_playback = Signal()

    def __init__(self, reader: FileReader, parent=None):
        super().__init__(parent)
        self.reader = reader
        self._stop_event = threading.Event()
        self._pending: list[np.ndarray] = []

    def _on_sample(self, row: np.ndarray):
        self._pending.append(row.copy())
        if len(self._pending) >= BATCH_SIZE:
            self.batch_ready.emit(np.stack(self._pending))
            self._pending = []

    def run(self):
        # QThread.run() is the code that actually executes on the new
        # thread — everything else in this class (the constructor, the
        # signals) runs on whichever thread created the QThread object.
        self.reader.run(on_sample=self._on_sample, stop_event=self._stop_event)
        if self._pending:
            self.batch_ready.emit(np.stack(self._pending))
            self._pending = []
        self.finished_playback.emit()

    def stop(self):
        self._stop_event.set()
        self.wait()
