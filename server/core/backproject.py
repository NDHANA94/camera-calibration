"""Back-projection geometry: turn a pixel into a 3D world ray.

Given a camera's intrinsics (K, distortion) and an extrinsic pose that maps
world coordinates into the camera frame (OpenCV convention, same as the
``rvec``/``tvec`` produced by calibration: ``X_cam = R @ X_world + t``), the
ray through a pixel ``(u, v)`` is:

    direction_world = normalize( R^T @ K^-1 @ [u, v, 1] )
    origin_world    = -R^T @ t          (the camera centre in world coords)

Distortion is removed with ``cv2.undistortPoints`` before forming the ray.
"""
from __future__ import annotations

from typing import Sequence, Tuple

import cv2
import numpy as np


def back_project_ray(
    K: np.ndarray,
    dist: np.ndarray,
    uv: Sequence[float],
    rvec: Sequence[float],
    tvec: Sequence[float],
) -> Tuple[np.ndarray, np.ndarray]:
    """Return ``(origin, direction)`` of the world-space ray through pixel *uv*.

    Parameters
    ----------
    K, dist : the camera matrix (3x3) and distortion coefficients.
    uv      : pixel coordinate ``[u, v]`` (e.g. a bbox centre).
    rvec    : rotation vector (Rodrigues) of the world->camera extrinsic.
    tvec    : translation vector of the world->camera extrinsic.

    Returns
    -------
    origin    : 3-vector, the camera centre in world coordinates.
    direction : 3-vector, unit length, pointing from the camera through *uv*.
    """
    K = np.asarray(K, dtype=np.float64).reshape(3, 3)
    dist = np.asarray(dist, dtype=np.float64).reshape(-1)
    pts = np.array([[[float(uv[0]), float(uv[1])]]], dtype=np.float64)  # (1,1,2)
    norm = cv2.undistortPoints(pts, K, dist)  # normalized image coords
    dir_cam = np.array([norm[0, 0, 0], norm[0, 0, 1], 1.0], dtype=np.float64)

    R, _ = cv2.Rodrigues(np.asarray(rvec, dtype=np.float64).reshape(3))
    t = np.asarray(tvec, dtype=np.float64).reshape(3)

    direction = R.T @ dir_cam
    norm_len = np.linalg.norm(direction)
    if norm_len > 0:
        direction = direction / norm_len
    origin = -(R.T @ t)
    return origin, direction


# ---------------------------------------------------------------------------
# Stereo depth (disparity -> depth heatmap)
# ---------------------------------------------------------------------------

# Cache of rectification maps per (session_id, per-eye size) so we don't rebuild
# them on every captured shot.
_rectify_cache: dict = {}


def _rectify_maps(session_id: str, calib: dict):
    w, h = calib["size"]
    key = (session_id, w, h)
    maps = _rectify_cache.get(key)
    if maps is None:
        mapLx, mapLy = cv2.initUndistortRectifyMap(
            calib["K1"], calib["D1"], calib["R1"], calib["P1"], (w, h), cv2.CV_32FC1)
        mapRx, mapRy = cv2.initUndistortRectifyMap(
            calib["K2"], calib["D2"], calib["R2"], calib["P2"], (w, h), cv2.CV_32FC1)
        maps = (mapLx, mapLy, mapRx, mapRy)
        _rectify_cache[key] = maps
    return maps


def _disparity_to_depth(grayL: np.ndarray, grayR: np.ndarray, Q: np.ndarray):
    """Run SGBM and reproject to 3D; return (Z_mm, valid_mask).  Z is in the
    same units as the stereo baseline (mm), invalid pixels are NaN."""
    num_disp = 128  # must be a multiple of 16
    block = 5
    sgbm = cv2.StereoSGBM_create(
        minDisparity=0, numDisparities=num_disp, blockSize=block,
        P1=8 * 3 * block ** 2, P2=32 * 3 * block ** 2,
        disp12MaxDiff=1, uniquenessRatio=10,
        speckleWindowSize=100, speckleRange=32,
        mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
    )
    disparity = sgbm.compute(grayL, grayR).astype(np.float32) / 16.0
    pts3d = cv2.reprojectImageTo3D(disparity, Q)
    Z = pts3d[:, :, 2]
    valid = (disparity > 0) & np.isfinite(Z) & (Z > 0) & (Z < 1e5)
    Z = np.where(valid, Z, np.nan)
    return Z, valid


def stereo_depth(calib: dict, session_id: str, frame_bgr: np.ndarray,
                 bbox_center_left=None) -> dict:
    """Compute a depth map + colour heatmap from a side-by-side L|R frame.

    *calib* comes from :func:`load_stereo_calib`.  *bbox_center_left* is an
    optional ``(u, v)`` point in the (displayed) left-eye pixel coordinates; when
    given, the median depth in a small window around its rectified location is
    returned as ``depth_mm``.

    Returns ``{heatmap_jpeg: bytes, depth_mm, min_mm, max_mm}``.
    """
    w, h = calib["size"]
    full_h, full_w = frame_bgr.shape[:2]
    half = full_w // 2
    left = frame_bgr[:, :half]
    right = frame_bgr[:, half:2 * half]
    # Resize each eye to the calibration size so Q / rectification stay valid.
    sx, sy = w / float(half), h / float(full_h)
    left = cv2.resize(left, (w, h))
    right = cv2.resize(right, (w, h))

    mapLx, mapLy, mapRx, mapRy = _rectify_maps(session_id, calib)
    rectL = cv2.remap(left, mapLx, mapLy, cv2.INTER_LINEAR)
    rectR = cv2.remap(right, mapRx, mapRy, cv2.INTER_LINEAR)
    grayL = cv2.cvtColor(rectL, cv2.COLOR_BGR2GRAY)
    grayR = cv2.cvtColor(rectR, cv2.COLOR_BGR2GRAY)

    Z, valid = _disparity_to_depth(grayL, grayR, calib["Q"])

    # Heatmap: near = warm (red), far = cool (blue).  Stretch to the 5–95th
    # percentile of valid depth for contrast; invalid pixels are black.
    if valid.any():
        lo = float(np.nanpercentile(Z, 5))
        hi = float(np.nanpercentile(Z, 95))
    else:
        lo, hi = 0.0, 1.0
    span = max(hi - lo, 1e-6)
    norm = np.clip((Z - lo) / span, 0.0, 1.0)
    norm = np.where(valid, norm, 0.0)
    inv8 = ((1.0 - norm) * 255).astype(np.uint8)  # invert so near is hot
    heat = cv2.applyColorMap(inv8, cv2.COLORMAP_JET)
    heat[~valid] = (0, 0, 0)
    # Downscale the returned heatmap so the response stays light (the depth map
    # itself was computed at full resolution).
    MAX_W = 720
    if heat.shape[1] > MAX_W:
        scale = MAX_W / float(heat.shape[1])
        heat = cv2.resize(heat, (MAX_W, int(round(heat.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", heat, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    heatmap_jpeg = buf.tobytes() if ok else b""

    depth_mm = None
    if bbox_center_left is not None:
        # Map the left-eye point into rectified coordinates, then sample depth.
        u = float(bbox_center_left[0]) * sx
        v = float(bbox_center_left[1]) * sy
        pt = np.array([[[u, v]]], dtype=np.float64)
        rp = cv2.undistortPoints(pt, calib["K1"], calib["D1"],
                                 R=calib["R1"], P=calib["P1"])
        rx, ry = int(round(rp[0, 0, 0])), int(round(rp[0, 0, 1]))
        if 0 <= rx < w and 0 <= ry < h:
            x0, x1 = max(0, rx - 4), min(w, rx + 5)
            y0, y1 = max(0, ry - 4), min(h, ry + 5)
            window = Z[y0:y1, x0:x1]
            if np.isfinite(window).any():
                depth_mm = float(np.nanmedian(window))

    return {
        "heatmap_jpeg": heatmap_jpeg,
        "depth_mm": depth_mm,
        "min_mm": (float(np.nanmin(Z)) if valid.any() else None),
        "max_mm": (float(np.nanmax(Z)) if valid.any() else None),
        # The percentile bounds used to colour the heatmap -- these label the
        # colorbar (near = lo, far = hi).
        "lo_mm": (lo if valid.any() else None),
        "hi_mm": (hi if valid.any() else None),
    }
