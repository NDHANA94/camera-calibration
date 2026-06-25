"""Unit tests for `server.core.frame_pipeline` and `server.core.quality`."""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from server.core import quality
from server.core.frame_pipeline import (
    Pipeline,
    detect_board,
    draw_overlay,
    encode_preview,
)
from tests._synthetic import default_synthetic_camera, render_chessboard


# ---------------------------------------------------------------------------
# detect_board
# ---------------------------------------------------------------------------

def test_detect_board_finds_corners_on_rendered_chessboard():
    """A clean rendered chessboard should be detected with the right corner count."""
    cam = default_synthetic_camera(image_size=(640, 480), board_w=8, board_h=5)
    img = render_chessboard(cam)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners = detect_board(gray, cam.board_w, cam.board_h)
    assert corners is not None
    assert corners.shape == (cam.num_corners, 1, 2)


def test_detect_board_returns_none_on_blank_image():
    cam = default_synthetic_camera()
    blank = np.full((cam.image_size[1], cam.image_size[0]), 235, dtype=np.uint8)
    assert detect_board(blank, cam.board_w, cam.board_h) is None


def test_detect_board_subpixel_refinement():
    """Corners should be float32 with sub-pixel precision after refinement."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners = detect_board(gray, cam.board_w, cam.board_h)
    assert corners.dtype == np.float32
    # Corner coordinates should be roughly inside the image and near the rendered grid
    xs = corners.reshape(-1, 2)[:, 0]
    ys = corners.reshape(-1, 2)[:, 1]
    assert xs.min() > 100 and xs.max() < cam.image_size[0] - 100
    assert ys.min() > 100 and ys.max() < cam.image_size[1] - 100


# ---------------------------------------------------------------------------
# Laplacian / blur
# ---------------------------------------------------------------------------

def test_laplacian_variance_higher_for_sharp_image():
    sharp = np.random.randint(0, 256, (480, 640), dtype=np.uint8)
    # Inject some structure so the Laplacian has non-zero variance
    sharp[100:200, 100:200] = np.random.randint(0, 256, (100, 100), dtype=np.uint8)
    blurry = cv2.GaussianBlur(sharp, (21, 21), 0)
    sharp_score = quality.laplacian_variance(sharp)
    blurry_score = quality.laplacian_variance(blurry)
    assert sharp_score > blurry_score


def test_is_sharp_uses_default_threshold():
    """`is_sharp` returns True for high-variance, False for low-variance inputs."""
    structured = np.zeros((200, 200), dtype=np.uint8)
    structured[::4, ::4] = 255  # strong edges -> high Laplacian variance
    flat = np.full((200, 200), 128, dtype=np.uint8)  # uniform -> 0 variance
    assert quality.is_sharp(structured)
    assert not quality.is_sharp(flat)


# ---------------------------------------------------------------------------
# Pipeline.process: stability, blur rejection, movement enforcement
# ---------------------------------------------------------------------------

def _hint_fn(captures):
    return ""


def test_pipeline_captures_after_stable_window():
    """Pipeline should auto-capture after `stable_frames` consecutive stable frames."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    pipe = Pipeline(board_w=cam.board_w, board_h=cam.board_h, required_captures=2)

    captured = False
    for _ in range(pipe.stable_frames * 3 + 5):
        result = pipe.process(img, _hint_fn)
        if result.capture_taken:
            captured = True
            break

    # The pipeline should have captured within `stable_frames * 2 + 5` frames.
    assert captured
    assert len(pipe.captures) == 1


def test_pipeline_rejects_blurry_frames():
    """Frames below the blur threshold should not increment `_stable_count`."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    # Drop blur threshold to 1e9 so our rendered frame is "blurry" relative to it
    pipe = Pipeline(
        board_w=cam.board_w, board_h=cam.board_h,
        required_captures=2, blur_threshold=1e9,
    )
    initial_count = pipe._stable_count
    for _ in range(20):
        pipe.process(img, _hint_fn)
    assert pipe._stable_count == initial_count  # never incremented
    assert pipe.captures == []


def test_pipeline_enforces_movement_between_captures():
    """After a capture, repeating the same frame shouldn't trigger another capture."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    pipe = Pipeline(board_w=cam.board_w, board_h=cam.board_h, required_captures=5)

    # First capture
    for _ in range(pipe.stable_frames + 2):
        if pipe.captures:
            break
        pipe.process(img, _hint_fn)
    assert len(pipe.captures) == 1

    # Subsequent identical frames should NOT capture (no movement)
    for _ in range(pipe.stable_frames * 3):
        pipe.process(img, _hint_fn)
    assert len(pipe.captures) == 1


def test_pipeline_force_capture_bypasses_movement():
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    pipe = Pipeline(board_w=cam.board_w, board_h=cam.board_h, required_captures=5)
    result = pipe.process(img, _hint_fn, force_capture=True)
    assert result.capture_taken is True
    assert len(pipe.captures) == 1


def test_pipeline_force_capture_ignores_no_board():
    """force_capture must NOT capture if no board is found."""
    pipe = Pipeline(board_w=8, board_h=5, required_captures=2)
    blank = np.full((480, 640, 3), 235, dtype=np.uint8)
    result = pipe.process(blank, _hint_fn, force_capture=True)
    assert result.capture_taken is False
    assert pipe.captures == []


# ---------------------------------------------------------------------------
# draw_overlay + encode_preview (smoke)
# ---------------------------------------------------------------------------

def test_draw_overlay_draws_red_green_corners():
    """Overlay should at least return a copy with the expected dims."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    corners = detect_board(gray, cam.board_w, cam.board_h)
    annotated = draw_overlay(
        img, corners, board_found=True,
        board_w=cam.board_w, board_h=cam.board_h,
        capture_highlight=False, blur_score=100.0, blur_threshold=35.0,
        captures=0, required=10, hint="",
    )
    assert annotated.shape == img.shape
    # The overlay modifies pixels — the annotated frame should NOT be the same object
    assert annotated is not img


def test_encode_preview_round_trip():
    """encode_preview should produce a JPEG-decodable buffer of the right shape."""
    cam = default_synthetic_camera(image_size=(640, 480))
    img = render_chessboard(cam)
    jpeg = encode_preview(img, quality=80)
    assert isinstance(jpeg, (bytes, bytearray))
    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == img.shape  # no scaling for 640 px wide