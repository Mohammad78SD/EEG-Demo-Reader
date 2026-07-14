import os
from pathlib import Path
from dotenv import load_dotenv

# Load `.env` file automatically
load_dotenv()

# Configuration constants with type casting
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", 16))
SAMPLE_INTERVAL_S = float(os.environ.get("SAMPLE_INTERVAL_S", 0.002))
SAMPLE_INTERVAL_MS = int(SAMPLE_INTERVAL_S * 1000)
WINDOW = int(os.environ.get("WINDOW", 2500))
REDRAW_MS = int(os.environ.get("REDRAW_MS", 33))
DEFAULT_VISIBLE = int(os.environ.get("DEFAULT_VISIBLE", 3))
ORG = os.environ.get("ORG", "negand")
APP = os.environ.get("APP", "eeg-dashboard")
USE_OPENGL = os.environ.get("USE_OPENGL", "True").lower() in ("true", "1", "yes")

# Plot limits
Y_MIN = int(os.environ.get("Y_MIN", -60))
Y_MAX = int(os.environ.get("Y_MAX", 60))

# Path to the data file
DATA_FILE = Path(__file__).parent / os.environ.get("DATA_FILE", "EEG3840 Sine.txt")
