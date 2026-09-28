"""X-Ray scenario driver for F2 (runs INSIDE the API container, started by `xray_trace.py run --driver ...`).
Standard library only, so it also works in slim images without curl.
Waits until the API answers /health, POSTs a face image to /enroll, then GETs the stored image back.

  python /xray/xray_drivers/enroll.py PORT "Name" [IMAGE]      (IMAGE default: data/test_face.jpg)
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

port = sys.argv[1] if len(sys.argv) > 1 else "5002"
name = sys.argv[2] if len(sys.argv) > 2 else "X-Ray User"
image = sys.argv[3] if len(sys.argv) > 3 else "data/test_face.jpg"
base = f"http://127.0.0.1:{port}"

for i in range(90):
    try:
        with urllib.request.urlopen(base + "/health", timeout=2):
            break
    except (urllib.error.URLError, OSError):
        time.sleep(1)
else:
    sys.exit(f"[driver] API on port {port} never became healthy")
print(f"[driver] API is up on port {port}", flush=True)

boundary = uuid.uuid4().hex
with open(image, "rb") as fh:
    img = fh.read()
body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\n{name}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"face.jpg\"\r\n"
        f"Content-Type: image/jpeg\r\n\r\n").encode() + img + f"\r\n--{boundary}--\r\n".encode()
req = urllib.request.Request(base + "/enroll", data=body, method="POST",
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode())
except urllib.error.HTTPError as e:
    sys.exit(f"[driver] enroll failed: HTTP {e.code} {e.read()[:300]!r}")
print(f"[driver] enroll -> {json.dumps(resp)}", flush=True)
face_id = resp.get("face_id")
if not face_id:
    sys.exit("[driver] no face_id in the response")
with urllib.request.urlopen(f"{base}/media/uploads/{face_id}.jpg", timeout=10) as r:
    print(f"[driver] GET /media/uploads/{face_id}.jpg -> HTTP {r.status}", flush=True)
    sys.exit(0 if r.status == 200 else 1)
