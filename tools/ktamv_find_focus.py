#!/usr/bin/env python3
"""
Encontra o melhor valor de focus_absolute para a Nozzle Cam (kTAMV).

Antes de correr:
  - bico do T0 limpo, centrado sobre a camara, a altura de foco (Z do tool_align_macro)
  - LEDs da Nozzle Cam ligados como ficam durante a calibracao
Uso:
  python3 ktamv_find_focus.py
"""
import subprocess, time, urllib.request
import cv2, numpy as np

DEVICE = "/dev/v4l/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0-video-index0"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
SETTLE_S = 0.8          # tempo para a lente se mover e sair um frame novo
FOCUS_MIN, FOCUS_MAX = 0, 1023


def v4l2_set(**ctrls):
    arg = ",".join("%s=%s" % kv for kv in ctrls.items())
    subprocess.run(["v4l2-ctl", "-d", DEVICE, "-c", arg], check=True)


def grab_gray():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        data = np.frombuffer(r.read(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA)  # igual ao servidor kTAMV
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def sharpness_peak(gray, k=15):
    """Mesma metrica do detetor: pico da variancia local do Laplaciano."""
    g = cv2.GaussianBlur(gray.astype(np.float32), (3, 3), 0)
    lap = cv2.Laplacian(g, cv2.CV_32F, ksize=3)
    mean = cv2.blur(lap, (k, k))
    var = np.sqrt(np.maximum(cv2.blur(lap * lap, (k, k)) - mean * mean, 0))
    return float(cv2.GaussianBlur(var, (0, 0), 5).max())


def measure(focus):
    v4l2_set(focus_absolute=focus)
    time.sleep(SETTLE_S)
    grab_gray()                      # descarta um frame por seguranca
    vals = [sharpness_peak(grab_gray()) for _ in range(2)]
    return sum(vals) / len(vals)


def sweep(values):
    results = []
    for f in values:
        s = measure(f)
        results.append((s, f))
        print("  focus_absolute=%4d  nitidez=%6.2f" % (f, s))
    return max(results)


def main():
    print("A desligar o autofoco...")
    v4l2_set(focus_automatic_continuous=0)
    time.sleep(0.5)

    print("Passagem grossa:")
    _, best = sweep(range(FOCUS_MIN, FOCUS_MAX + 1, 32))

    print("Passagem fina a volta de %d:" % best)
    lo, hi = max(FOCUS_MIN, best - 32), min(FOCUS_MAX, best + 32)
    score, best = sweep(range(lo, hi + 1, 4))

    v4l2_set(focus_absolute=best)
    print("\nMelhor: focus_absolute=%d (nitidez %.2f). A camara ficou neste valor." % (best, score))
    print("Confirma na preview e diz o valor para o crowsnest.conf.")


if __name__ == "__main__":
    main()
