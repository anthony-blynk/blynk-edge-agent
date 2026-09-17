FROM ubuntu:22.04

# pip, not apt, for opencv here: Ubuntu 22.04's apt-packaged OpenCV (4.5.4,
# one of the first releases to include FaceDetectorYN at all) fails to
# parse newer YuNet model exports - confirmed via "Layer with requested
# id=-1 not found" trying to load face_detection_yunet_2023mar.onnx. A
# current pip-installed OpenCV avoids that. This is an isolated container,
# not the shared host, so pip here doesn't have the PEP 668 conflict risk
# that applies on the host OS itself.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir opencv-python-headless paho-mqtt

WORKDIR /app

# YuNet: a real DNN-based face detector (OpenCV's own model zoo), not the
# old Haar cascade - noticeably more accurate than Haar cascade's
# per-frame flicker-prone detections.
RUN curl -fsSL -o face_detection_yunet_2023mar.onnx \
    https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

COPY camera_detect.py .

CMD ["python3", "-u", "camera_detect.py"]
