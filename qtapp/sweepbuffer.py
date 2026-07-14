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

    def push_until_full(self, batch: np.ndarray):
        """batch: shape (n, num_channels), row i = sample i.

        Writes rows until the window is full, then stops WITHOUT clearing —
        clearing here would wipe rows from this same batch before the
        caller ever got a chance to draw them. Returns the unwritten
        leftover rows if the window filled mid-batch (caller must flush a
        draw + call reset() + push the leftover), or None if the whole
        batch fit with room to spare.
        """
        for i, row in enumerate(batch):
            if self.position == self.window:
                return batch[i:]
            self.data[:, self.position] = row
            self.position += 1
        return None

    def reset(self):
        self.data.fill(np.nan)
        self.position = 0
