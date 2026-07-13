import numpy as np


class SweepBuffer:
    """Fixed-position sweep window, one row per channel. On wrap, the whole
    window clears and drawing restarts at x=0 (no erase-ahead trace).

    Plain numpy, no Qt — lives on the main thread, fed only from the
    batch-ready slot, so nothing here needs a lock.
    """

    def __init__(self, num_channels: int, window: int, sample_interval_ms: float):
        self.num_channels = num_channels
        self.window = window
        self.data = np.full((num_channels, window), np.nan, dtype=np.float32)
        self.xs = np.arange(window, dtype=np.float32) * sample_interval_ms
        self.position = 0

    def push(self, batch: np.ndarray):
        """batch: shape (n, num_channels), row i = sample i."""
        for row in batch:
            if self.position == self.window:
                self.data.fill(np.nan)
                self.position = 0
            self.data[:, self.position] = row
            self.position += 1

    def reset(self):
        self.data.fill(np.nan)
        self.position = 0
