"""Vendored Kokoro 0.9.4 runtime under Apache-2.0.

PhoneAgent intentionally avoids upstream's process-global Loguru reconfiguration.
"""

from .model import KModel
from .pipeline import KPipeline

__all__ = ["KModel", "KPipeline"]
__version__ = "0.9.4-phoneagent1"
