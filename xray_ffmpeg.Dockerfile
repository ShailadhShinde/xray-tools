# Used by videos.bat (written by xray_runfile.py) to convert videos the app's image cannot read. Built once:
#   docker build -t xray-ffmpeg - < xray_ffmpeg.Dockerfile
# imageio-ffmpeg = a ready-made static ffmpeg for Linux (with the H.264 encoder libx264), installed by pip.
FROM python:3.12-slim
RUN pip install --no-cache-dir imageio-ffmpeg
