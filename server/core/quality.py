"""Frame-quality checks used by the auto-capture loop."""
from __future__ import annotations

import cv2
import numpy as np

# Engineering defaults. Not persisted to profiles per the planning decision.
DEFAULT_BLUR_THRESHOLD = 35.0  # Laplacian variance; below = too blurry
DEFAULT_STABLE_FRAMES = 5      # frames the board must be stable before auto-capture


def laplacian_variance(gray: np.ndarray) -> float:
    """Lower = blurrier. Use the variance of the Laplacian as a focus metric."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def is_sharp(gray: np.ndarray, threshold: float = DEFAULT_BLUR_THRESHOLD) -> bool:
    return laplacian_variance(gray) >= threshold