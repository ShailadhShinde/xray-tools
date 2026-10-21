# Used by videos.bat (written by xray_runfile.py) to convert videos the app's image cannot read. Built once:
#   docker build -t xray-ffmpeg:2 - < xray_ffmpeg.Dockerfile
# ffmpeg: linuxserver's build (Ubuntu 24.04) - it has the CPU encoder libx264 AND NVIDIA's GPU encoder h264_nvenc,
# plus the AV1 decoder dav1d. The GPU is used only when the container gets one with its video part:
#   docker run --gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility ...
# ("video" = the driver's encoder / decoder libraries; --gpus all alone gives only compute + utility). Without a GPU
# xray_videos.py converts on the CPU with the same ffmpeg.
# Python (for xray_videos.py): copied from the official Python image. Debian 12 there has an older C library than
# Ubuntu 24.04 here, so the copied python runs here (the other way round it would not).
FROM python:3.12-slim-bookworm AS py
FROM linuxserver/ffmpeg:7.1.1
COPY --from=py /usr/local/bin/python3.12 /usr/local/bin/python3.12
COPY --from=py /usr/local/lib/libpython3.12.so.1.0 /usr/local/lib/
COPY --from=py /usr/local/lib/python3.12 /usr/local/lib/python3.12
RUN ln -s python3.12 /usr/local/bin/python3 && ln -s python3.12 /usr/local/bin/python && ldconfig
# the base image starts ffmpeg itself; here the command line decides
ENTRYPOINT []
