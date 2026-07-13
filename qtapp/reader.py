import time
from pathlib import Path

import numpy as np

DATA_FILE = Path(__file__).parent / "EEG3840 Sine.txt"
SAMPLE_INTERVAL_S = 0.002  # 500Hz


class FileReader:
    """Plays back EEG3840 Sine.txt at a drift-corrected 2ms cadence, once through."""

    def __init__(self, path: Path = DATA_FILE):
        with open(path) as f:
            self.channels = f.readline().split()
        self.data = np.loadtxt(path, skiprows=1, dtype=np.float32)
        self.num_channels = self.data.shape[1]
        self.num_rows = self.data.shape[0]

    def run(self, on_sample, stop_event):
        """Blocking loop; calls on_sample(row: np.ndarray[float32]) once per 2ms tick.
        Stops after one pass through the file (row_idx reaches num_rows)."""
        start = time.perf_counter()
        tick = 0
        row_idx = 0
        while not stop_event.is_set() and row_idx < self.num_rows:
            on_sample(self.data[row_idx])
            row_idx += 1
            tick += 1
            target = start + tick * SAMPLE_INTERVAL_S
            sleep_for = target - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)
