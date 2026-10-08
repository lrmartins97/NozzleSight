#!/usr/bin/env python3
"""
Escolhe o exposure_time_absolute da Nozzle Cam pelo desempenho real do detetor kTAMV.

Para cada exposicao: tira varios frames, corre a detecao do ktamv_server_dm.py
(a mesma que corre na calibracao) e mede:
  - detecoes  : quantos frames encontraram o bico (tem de ser todos)
  - jitter    : quanto o centro varia entre frames, em pixeis (quanto menor melhor)
  - saturado  : % de pixeis brancos "queimados" na zona do bico (quanto menor melhor)

Antes de correr:
  - T0 com bico limpo em X22 Y113 Z8.5, LEDs ligados como na calibracao
Uso:
  python3 ktamv_find_exposure.py
"""
import os, subprocess, sys, time, urllib.request
import cv2, numpy as np

# Codigo do servidor: a pasta server/ ao lado desta (repositorio), ou
# ~/kTAMV/server se o script for copiado para outro sitio.
_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.normpath(os.path.join(_HERE, "..", "server"))
if not os.path.isfile(os.path.join(SERVER_DIR, "ktamv_server_dm.py")):
    SERVER_DIR = os.path.expanduser("~/kTAMV/server")
sys.path.insert(0, SERVER_DIR)
from ktamv_server_dm import Ktamv_Server_Detection_Manager as DM

DEVICE = "/dev/v4l/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0-video-index0"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
EXPOSURES = [40, 60, 80, 100, 130, 166, 200, 250, 300, 400]
FRAMES_PER_VALUE = 8
SETTLE_S = 0.8


def v4l2_set(**ctrls):
    arg = ",".join("%s=%s" % kv for kv in ctrls.items())
    subprocess.run(["v4l2-ctl", "-d", DEVICE, "-c", arg], check=True)


def grab_gray():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        data = np.frombuffer(r.read(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA)  # igual ao servidor kTAMV
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def evaluate(dm, exposure):
    v4l2_set(exposure_time_absolute=exposure)
    time.sleep(SETTLE_S)
    grab_gray()                                   # descarta um frame
    centers, sat = [], []
    for _ in range(FRAMES_PER_VALUE):
        gray = grab_gray()
        res, mask = dm.focusedNozzleDetection(gray)
        if mask is not None:
            roi = gray[mask > 0]
            sat.append(100.0 * np.count_nonzero(roi >= 250) / max(roi.size, 1))
        if res is not None:
            centers.append(res[:2])
    ok = len(centers)
    jitter = float(np.linalg.norm(np.std(centers, axis=0))) if ok >= 2 else float("inf")
    saturated = float(np.mean(sat)) if sat else float("nan")
    return ok, jitter, saturated


def main():
    dm = DM(log=lambda m: None, camera_url=SNAPSHOT_URL, cloud_url="")
    v4l2_set(auto_exposure=1)
    print("exposicao  detecoes  jitter(px)  saturado(%)")
    results = []
    for e in EXPOSURES:
        ok, jitter, saturated = evaluate(dm, e)
        results.append((e, ok, jitter, saturated))
        print("  %4d       %d/%d      %6.2f      %6.1f" % (e, ok, FRAMES_PER_VALUE, jitter, saturated))

    good = [r for r in results if r[1] == FRAMES_PER_VALUE]
    if not good:
        print("\nNenhuma exposicao detetou o bico em todos os frames. Confirma posicao, Z e limpeza do bico.")
        v4l2_set(exposure_time_absolute=166)
        return
    # menor jitter; em empate (diferenca < 0.1 px) prefere menos saturacao
    best_jitter = min(r[2] for r in good)
    close = [r for r in good if r[2] - best_jitter < 0.1]
    best = min(close, key=lambda r: r[3])
    v4l2_set(exposure_time_absolute=best[0])
    print("\nMelhor: exposure_time_absolute=%d (jitter %.2f px, saturado %.1f%%). A camara ficou neste valor."
          % (best[0], best[2], best[3]))


if __name__ == "__main__":
    main()
