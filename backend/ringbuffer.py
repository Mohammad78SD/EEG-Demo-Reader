import threading

import numpy as np


class RingBuffer:
    """Fixed-size numpy ring buffer, shape (capacity, num_channels)."""

    def __init__(self, capacity: int, num_channels: int):
        self.capacity = capacity
        self.buf = np.zeros((capacity, num_channels), dtype=np.float32)
        self.write_idx = 0
        self.filled = 0
        self.lock = threading.Lock()

    def reset(self):
        with self.lock:
            self.buf.fill(0)
            self.write_idx = 0
            self.filled = 0

    def push(self, row: np.ndarray):
        with self.lock:
            self.buf[self.write_idx] = row
            self.write_idx = (self.write_idx + 1) % self.capacity
            self.filled = min(self.filled + 1, self.capacity)

    def latest(self, n: int) -> np.ndarray:
        """Returns the last n rows in chronological order (oldest first). May return fewer if not filled yet."""
        with self.lock:
            n = min(n, self.filled)
            if n == 0:
                return np.empty((0, self.buf.shape[1]), dtype=np.float32)
            end = self.write_idx
            start = (end - n) % self.capacity
            if start < end:
                return self.buf[start:end].copy()
            return np.concatenate([self.buf[start:], self.buf[:end]])
