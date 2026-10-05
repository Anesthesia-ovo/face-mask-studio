"""Exercise the bundled executable without requiring internet or private files."""
import json
import socket
import tempfile
import threading
import traceback
from pathlib import Path

import cv2
import numpy as np

from engine import FaceEngine, MediaSelection, Settings, process_file, read_preview


def run(report_path):
    checks = []
    result = {"passed": False, "checks": checks}
    try:
        try:
            socket.create_connection(('example.com', 443), timeout=0.01)
            raise AssertionError('Python networking guard inactive')
        except OSError:
            checks.append('Python network connections blocked')
        engine = FaceEngine()
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        frame[65:145, 100:180] = np.random.default_rng(123).integers(20, 240, (80, 80, 3), dtype=np.uint8)
        settings = Settings(mode='all', effect='mosaic')
        faces = engine.detect(frame, settings)
        assert np.asarray(faces).shape[-1] == 15
        checks.append('Bundled YuNet loads and runs')
        selected = MediaSelection(manual_boxes=[(100, 65, 80, 80)], reference_frame=frame, anchor_frame=0)
        masked = engine.preview(frame, settings, selected)
        assert masked.shape == frame.shape
        assert np.mean(np.abs(masked[70:140, 110:170].astype(float) - frame[70:140, 110:170])) > 5
        checks.append('Manual mosaic changes selected pixels')
        report_path = Path(report_path).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='selfcheck_', dir=str(report_path.parent)) as folder:
            folder = Path(folder)
            source = folder / '测试图片.png'
            success, encoded = cv2.imencode('.png', frame)
            assert success
            encoded.tofile(str(source))
            loaded, metadata = read_preview(source)
            assert loaded.shape == frame.shape
            output = process_file(source, folder / 'processed', settings, selected, threading.Event(), lambda *_: None)
            output_path = Path(output.get('output', output.get('path', '')))
            assert output_path.is_file()
            assert source.read_bytes() == encoded.tobytes()
            checks.append('Unicode image input/output and original preservation')
            writer = cv2.VideoWriter(str(folder / 'moving.avi'), cv2.VideoWriter_fourcc(*'MJPG'), 12, (320,240))
            assert writer.isOpened()
            for index in range(12):
                affine = np.float32([[1,0,index*2],[0,1,0]])
                writer.write(cv2.warpAffine(frame, affine, (320,240)))
            writer.release()
            movie = process_file(folder / 'moving.avi', folder / 'processed', settings, selected, threading.Event(), lambda *_: None)
            video_path = Path(movie.get('output', movie.get('path', '')))
            assert video_path.is_file()
            cap = cv2.VideoCapture(str(video_path))
            count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            ok, first = cap.read()
            cap.release()
            assert ok and count == 12 and first.shape[:2] == (240,320)
            checks.append('Bundled FFmpeg MP4 encoder and moving rectangle tracking')
        result['passed'] = True
        result['opencv'] = cv2.__version__
    except Exception:
        result['error'] = traceback.format_exc()
    Path(report_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
