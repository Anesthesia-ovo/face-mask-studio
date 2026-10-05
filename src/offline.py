"""Runtime guard: this desktop application accepts local media only."""
import os

# Restrict the bundled OpenCV FFmpeg reader, including external references that
# might be embedded inside a local media container. Set before importing cv2.
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "protocol_whitelist;file,pipe|format_whitelist;mov,matroska,avi,asf,mpeg,mpegts"
os.environ["OPENCV_VIDEOIO_PRIORITY_GSTREAMER"] = "0"


def enable():
    import socket

    def denied(*args, **kwargs):
        raise OSError("此软件仅在本机处理文件，已禁用 Python 网络连接。")

    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.socket.sendto = denied
    if hasattr(socket.socket, "sendmsg"):
        socket.socket.sendmsg = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
