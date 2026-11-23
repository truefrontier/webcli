"""On-device face counts for `photos --max-faces`. Optional: `pip install 'truefrontier-webcli[faces]'`.

OpenCV's Haar cascade ships with the package and runs entirely on this machine. Nothing here opens a
socket, calls a cloud API, or reads the Facebook session. A miss (a face in profile, a small face) is a
lower count, and a photo is dropped only when the count is greater than N.
"""

from __future__ import annotations

from webcli.core.errors import UsageError

_HINT = "pip install 'truefrontier-webcli[faces]'  (on-device OpenCV; no cloud API)"


def _cv2():
    try:
        import cv2
    except ImportError:
        return None
    return cv2


def available() -> bool:
    return _cv2() is not None


def require() -> None:
    """Exit 2, before any photo is fetched, when the extra isn't installed."""
    if _cv2() is None:
        raise UsageError("Face counting needs the optional on-device detector (OpenCV). It is not installed.",
                         hint=_HINT)


def count_faces(data: bytes) -> int:
    """How many frontal faces OpenCV finds in `data`. ValueError when the bytes aren't an image."""
    cv2 = _cv2()
    if cv2 is None:
        require()
    import numpy as np

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    cascade = cv2.CascadeClassifier(cascade_path)
    if cascade.empty():
        raise RuntimeError("OpenCV couldn't load its face cascade. Reinstall the faces extra: " + _HINT)
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("not an image")
    found = cascade.detectMultiScale(image, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
    return int(len(found))
