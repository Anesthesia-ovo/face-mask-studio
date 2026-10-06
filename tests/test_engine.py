"""Run: python -m unittest discover -s tests -p test_engine.py -v

Pure image/tracking tests run without model downloads. Tests requiring the real
detector or Windows FFmpeg skip when those build resources are absent. A local
single-face portrait at tests/fixtures/face.jpg optionally enables identity QA.
No test makes any network request or downloads a portrait.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src"
FFMPEG = ROOT / "assets" / "ffmpeg.exe"
sys.path.insert(0, str(APP))
import engine


def face_row(x, y, w, h):
    return np.asarray([x, y, w, h, x + w * .3, y + h * .35,
                       x + w * .7, y + h * .35, x + w * .5, y + h * .55,
                       x + w * .35, y + h * .8, x + w * .65, y + h * .8, .99], np.float32)


class ColoredFaces(engine.FaceEngine):
    """Deterministic local fixture detector; integration also tests real YuNet below."""
    def __init__(self, model_dir=None, ffmpeg_path=None):
        self.detector = None
        self.recognizer = None
        self.ffmpeg_path = Path(ffmpeg_path) if ffmpeg_path else FFMPEG
        self._reference_key = None
        self._reference_features = []

    def detect(self, frame, settings):
        mask = ((frame[:, :, 2] > 180) & (frame[:, :, 1] < 100) & (frame[:, :, 0] < 100)).astype(np.uint8)
        count, _, boxes, _ = cv2.connectedComponentsWithStats(mask)
        faces = [face_row(*box[:4]) for box in boxes[1:] if box[4] > 30]
        return np.asarray(faces, dtype=np.float32).reshape(-1, 15)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        model_dir = ROOT / "models"
        cls.actual = (engine.FaceEngine(model_dir=model_dir, ffmpeg_path=FFMPEG)
                      if (model_dir / "face_detection_yunet_2023mar.onnx").is_file() else None)
        cls.fake = ColoredFaces()

    def test_real_model_unicode_blank(self):
        if self.actual is None:
            self.skipTest("offline detection model has not been prepared")
        self.assertEqual(self.actual.detect(np.zeros((91, 121, 3), np.uint8), engine.Settings()).shape, (0, 15))

    def test_real_face_identity_after_translation(self):
        sample = ROOT / "tests" / "fixtures" / "face.jpg"
        if not sample.is_file() or self.actual is None or self.actual.recognizer is None:
            self.skipTest("real face/model fixture absent")
        original, _ = engine.read_preview(sample)
        settings = engine.Settings(mode="selected")
        original_faces = self.actual.detect(original, settings)
        self.assertEqual(len(original_faces), 1)
        height, width = original.shape[:2]
        moved = np.zeros((height + 160, width + 340, 3), np.uint8)
        moved[80:80 + height, 170:170 + width] = original
        moved_faces = self.actual.detect(moved, settings)
        self.assertEqual(len(moved_faces), 1)
        selection = engine.MediaSelection([original_faces[0]], [], original.copy())
        self.assertEqual(self.actual.keep_indices(moved, settings, selection, moved_faces), {0})
        self.assertEqual(self.actual.keep_flags(moved, settings, selection, moved_faces), [True])

    def test_renamed_executable_image_format_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "renamed.png"
            source.write_bytes(b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 20 20\nshowpage\n")
            with self.assertRaises(engine.EngineError):
                engine.read_preview(source)

    def test_local_paths_reject_network(self):
        for source in ("https://example.com/movie.mp4", "\\\\server\\share\\x.mp4", "//server/share/x.mp4"):
            with self.assertRaises(engine.EngineError):
                engine._local_file(source)

    def test_image_mask_keep_alpha_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="image-test-") as temporary:
            folder = Path(temporary)
            pixels = np.full((91, 121, 4), (20, 20, 20, 200), np.uint8)
            pixels[20:45, 30:55, :3] = (240, 10, 10)
            pixels[20:65, 70:110, :3] = (240, 10, 10)
            source = folder / "原图片.png"
            Image.fromarray(pixels).save(source)
            settings = engine.Settings(effect="solid", mode="largest", padding=0)
            first = self.fake.process_file(source, folder, settings)
            second = self.fake.process_file(source, folder, settings)
            self.assertNotEqual(first["output"], second["output"])
            self.assertTrue(source.exists())
            result = np.asarray(Image.open(first["output"]))
            self.assertTrue(np.all(result[25:40, 35:50, :3] == 0))
            self.assertTrue(np.all(result[25:60, 75:105, 0] == 240))
            self.assertTrue(np.all(result[:, :, 3] == 200))
            self.assertFalse(list(folder.glob(".part-*")))

    def test_exif_orientation(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "方向.jpg"
            image = Image.new("RGB", (60, 30), "white")
            exif = Image.Exif()
            exif[274] = 6
            image.save(source, exif=exif)
            frame, info = engine.read_preview(source)
            self.assertEqual(frame.shape[:2], (60, 30))
            self.assertEqual((info["width"], info["height"]), (30, 60))

    def test_camera_mpo_jpg_processes_primary_image(self):
        # Camera JPGs can contain a primary JPEG plus an auxiliary image.
        # This generated fixture needs no private photographs or model files.
        with tempfile.TemporaryDirectory(prefix="mpo-test-") as temporary:
            folder = Path(temporary)
            source = folder / "相机主图.JPG"
            primary = Image.new("RGB", (64, 48), (20, 160, 20))
            auxiliary = Image.new("RGB", (32, 24), (20, 20, 240))
            primary.save(source, format="MPO", save_all=True, append_images=[auxiliary],
                         quality=100, subsampling=0)
            original = source.read_bytes()
            with Image.open(source) as container:
                self.assertEqual((container.format, container.n_frames), ("MPO", 2))
            frame, info = engine.read_preview(source)
            self.assertEqual(frame.shape, (48, 64, 3))
            self.assertEqual((info["width"], info["height"]), (64, 48))
            self.assertEqual(info["source_format"], "MPO")
            self.assertEqual(info["source_frame_count"], 2)
            self.assertTrue(info["primary_only"])
            self.assertEqual(info["frame_count"], 1)
            self.assertLess(np.abs(frame[0, 0].astype(int) - [20, 160, 20]).max(), 4)
            self.assertEqual(len(engine._read_image(source)), 2)
            selection = engine.MediaSelection(manual_boxes=[(8, 8, 16, 16)])
            result = self.fake.process_file(source, folder / "out",
                                            engine.Settings(effect="solid", padding=0), selection)
            with Image.open(result["output"]) as final:
                self.assertEqual((final.format, final.size, final.mode), ("PNG", (64, 48), "RGB"))
                pixels = np.asarray(final)
                self.assertTrue(np.all(pixels[10:20, 10:20] == 0))
                self.assertTrue(np.array_equal(pixels[0, 0], frame[0, 0, ::-1]))
            self.assertTrue(any("MPO" in message and "主图" in message and "HDR" in message
                                for message in result["warnings"]))
            self.assertEqual(source.read_bytes(), original)

    def test_other_multiframe_images_remain_rejected(self):
        # Allowing camera MPOs must not silently discard animation or TIFF pages.
        Image.init()
        formats = [("TIFF", ".tiff"), ("PNG", ".png"), ("WEBP", ".webp")]
        with tempfile.TemporaryDirectory(prefix="multiframe-test-") as temporary:
            folder = Path(temporary)
            for image_format, extension in formats:
                if image_format not in Image.SAVE_ALL:
                    continue
                with self.subTest(image_format=image_format):
                    source = folder / ("multiframe" + extension)
                    Image.new("RGB", (48, 32), "green").save(
                        source, format=image_format, save_all=True,
                        append_images=[Image.new("RGB", (48, 32), "blue")], duration=100, loop=0)
                    original = source.read_bytes()
                    with Image.open(source) as container:
                        self.assertEqual(container.n_frames, 2)
                    with self.assertRaisesRegex(engine.EngineError, "不支持多帧图片"):
                        engine.read_preview(source)
                    with self.assertRaisesRegex(engine.EngineError, "不支持多帧图片"):
                        self.fake.process_file(source, folder / "out", engine.Settings())
                    self.assertEqual(source.read_bytes(), original)
                    self.assertFalse((folder / "out").exists())

    def test_pre_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "x.png"
            Image.new("RGB", (20, 20)).save(source)
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(engine.CancelledError):
                self.fake.process_file(source, temporary, engine.Settings(), cancel_event=cancelled)
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_optical_flow_bridges_moving_missed_face(self):
        tracker = engine._VideoTracker(25)
        rng = np.random.default_rng(7)
        texture = rng.integers(50, 255, (34, 34, 3), dtype=np.uint8)
        for index in range(7):
            frame = np.zeros((120, 220, 3), np.uint8)
            x = 20 + index * 5
            frame[40:74, x:x + 34] = texture
            detected = np.asarray([face_row(x, 40, 34, 34)]) if index == 0 else np.empty((0, 15), np.float32)
            tracks = tracker.update(frame, detected, set(), [], False)
            self.assertEqual(len(tracks), 1)
            self.assertLess(abs(float(tracks[0].box[0]) - x), 2.0)
        self.assertEqual(tracker.tracks[0].missing, 6)

    def test_manual_anchor_and_scene_reset(self):
        tracker = engine._VideoTracker(25)
        blank = np.zeros((80, 100, 3), np.uint8)
        faces = np.empty((0, 15), np.float32)
        tracker.update(blank, faces, set(), [[20, 20, 25, 25]], False)
        self.assertFalse(tracker.manual)
        tracker.update(blank, faces, set(), [[20, 20, 25, 25]], True)
        self.assertEqual(len(tracker.manual), 1)
        tracker.update(np.full_like(blank, 255), faces, set(), [[20, 20, 25, 25]], False)
        self.assertFalse(tracker.manual)
        self.assertEqual(tracker.manual_lost, 1)

    def _make_video(self, folder):
        if os.name != "nt" or not FFMPEG.is_file():
            self.skipTest("bundled Windows FFmpeg has not been prepared")
        path = folder / "移动脸部原片.mp4"
        frames = []
        for index in range(10):
            frame = np.full((91, 121, 3), 30, np.uint8)
            x = 8 + index * 7
            frame[30:55, x:x + 22] = (10, 10, 240)
            frames.append(frame)
        command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "121x91", "-r", "10", "-i", "pipe:0",
                   "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1",
                   "-c:v", "libx264", "-pix_fmt", "yuv444p", "-c:a", "aac", "-shortest", str(path)]
        result = subprocess.run(command, input=b"".join(f.tobytes() for f in frames), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return path

    def test_video_motion_odd_dimensions_audio_unicode(self):
        with tempfile.TemporaryDirectory(prefix="video-test-") as temporary:
            folder = Path(temporary)
            source = self._make_video(folder)
            frame, info = engine.read_preview(source, 5)
            self.assertEqual(info["frame_index"], 5)
            self.assertEqual(frame.shape[:2], (91, 121))
            result = self.fake.process_file(source, folder / "输出", engine.Settings(effect="solid", padding=0))
            self.assertEqual(result["frame_count"], 10)
            self.assertEqual(result["face_count"], 10)
            capture = engine._capture(Path(result["output"]))
            index = 0
            while True:
                ok, output = capture.read()
                if not ok:
                    break
                self.assertEqual(output.shape[:2], (92, 122))
                self.assertLess(float(output[34:50, 10 + index * 7:25 + index * 7].mean()), 12)
                index += 1
            capture.release()
            self.assertEqual(index, 10)
            inspect = subprocess.run([str(FFMPEG), "-hide_banner", "-i", result["output"], "-f", "null", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertIn(b"Audio:", inspect.stderr)
            self.assertFalse(list((folder / "输出").glob(".part-*")))

    def test_video_cancel_removes_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = self._make_video(folder)
            cancelled = threading.Event()
            def progress(fraction, message):
                if fraction > 0:
                    cancelled.set()
            with self.assertRaises(engine.CancelledError):
                self.fake.process_file(source, folder / "out", engine.Settings(), cancel_event=cancelled, on_progress=progress)
            self.assertEqual(list((folder / "out").iterdir()), [])

    def test_encoder_failure_removes_partial(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            source = self._make_video(folder)
            engine_instance = ColoredFaces(ffmpeg_path=folder / "missing.exe")
            with self.assertRaises(engine.EngineError):
                engine_instance.process_file(source, folder / "out", engine.Settings())
            self.assertEqual(list((folder / "out").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
