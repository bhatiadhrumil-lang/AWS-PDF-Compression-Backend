"""Shared path bootstrap: import src/ modules without installing anything."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
