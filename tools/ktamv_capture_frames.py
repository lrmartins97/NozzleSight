#!/usr/bin/env python3
"""
Guarda imagens cruas da Nozzle Cam com varias exposicoes, para analise do detetor.

Uso:
  python3 ktamv_capture_frames.py 20 30 50 80 120
  (sem numeros usa: 20 30 50 80 120)

Antes: bico em cima da camara (ex.: T0 / G0 Z8.2 / G0 X22 Y113), luz como vai
ser usada na calibracao.
Resultado: pasta ~/ktamv_frames/ com um .jpg por exposicao (exp_020.jpg, ...).
No fim a camara volta a exposicao em que estava.
"""
import os, re, subprocess, sys, time, urllib.request

DEVICE = "/dev/v4l/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0-video-index0"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
DEFAULT_EXPOSURES = [20, 30, 50, 80, 120]
OUT = os.path.expanduser("~/ktamv_frames")


def v4l2_set(**ctrls):
    arg = ",".join("%s=%s" % kv for kv in ctrls.items())
    subprocess.run(["v4l2-ctl", "-d", DEVICE, "-c", arg], check=True)


def v4l2_get(name):
    out = subprocess.run(["v4l2-ctl", "-d", DEVICE, "--get-ctrl=" + name],
                         check=True, capture_output=True, text=True).stdout
    m = re.search(r":\s*(-?\d+)", out)
    return int(m.group(1)) if m else None


def grab():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        return r.read()


def main():
    exposures = [int(a) for a in sys.argv[1:]] or DEFAULT_EXPOSURES
    original = v4l2_get("exposure_time_absolute")
    os.makedirs(OUT, exist_ok=True)
    try:
        v4l2_set(auto_exposure=1)
        for e in exposures:
            v4l2_set(exposure_time_absolute=e)
            time.sleep(0.8)
            grab()                               # descarta um frame
            path = os.path.join(OUT, "exp_%03d.jpg" % e)
            with open(path, "wb") as f:
                f.write(grab())
            print("guardado", path)
    finally:
        if original is not None:
            v4l2_set(exposure_time_absolute=original)
            print("Camara reposta em exposure_time_absolute=%d." % original)


if __name__ == "__main__":
    main()
