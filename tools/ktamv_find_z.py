#!/usr/bin/env python3
"""
Encontra a melhor altura Z para a Nozzle Cam (a camara esta no prato e tem foco fixo,
por isso o Z e' o foco) e mostra a margem de Z com imagem nitida.

Para cada Z: move o prato via Moonraker, tira varios frames e mede
  - nitidez  : pico da nitidez local na ponta do bico (mesma metrica do detetor)
  - detecoes : em quantos frames o detetor do kTAMV encontrou o bico
  - jitter   : variacao do centro detetado entre frames, em pixeis

SEGURANCA: a camara esta no prato. Z mais baixo = prato mais perto do bico.
Antes de correr, confirma A MAO que "G0 Z<Z_MIN>" em cima da camara e' seguro.

Antes de correr:
  - G28, T0, bico limpo, LEDs ligados como na calibracao
Uso:
  python3 ktamv_find_z.py                -> gama por defeito (Z_MIN a Z_MAX)
  python3 ktamv_find_z.py 7.0 9.5        -> gama indicada (minimo e maximo)
"""
import os, json, sys, time, urllib.request, urllib.parse
import cv2, numpy as np

# Codigo do servidor: a pasta server/ ao lado desta (repositorio), ou
# ~/kTAMV/server se o script for copiado para outro sitio.
_HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.normpath(os.path.join(_HERE, "..", "server"))
if not os.path.isfile(os.path.join(SERVER_DIR, "ktamv_server_dm.py")):
    SERVER_DIR = os.path.expanduser("~/kTAMV/server")
sys.path.insert(0, SERVER_DIR)
from ktamv_server_dm import Ktamv_Server_Detection_Manager as DM

MOONRAKER = "http://localhost:7125"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
CAM_X, CAM_Y = 22.0, 113.0
Z_MIN, Z_MAX, Z_STEP = 7.5, 9.5, 0.1   # Z_MIN = o mais perto que o prato pode ir
FRAMES_PER_Z = 4
SETTLE_S = 0.8


def gcode(script):
    """Corre G-code no Klipper via Moonraker; so' volta quando o comando acaba."""
    url = MOONRAKER + "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def homed_axes():
    url = MOONRAKER + "/printer/objects/query?toolhead=homed_axes"
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)["result"]["status"]["toolhead"]["homed_axes"]


def grab_gray():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        data = np.frombuffer(r.read(), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA)  # igual ao servidor kTAMV
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def evaluate(dm, z):
    gcode("G0 Z%.2f F300\nM400" % z)
    time.sleep(SETTLE_S)
    grab_gray()                                   # descarta um frame
    sharp, centers = [], []
    for _ in range(FRAMES_PER_Z):
        gray = grab_gray()
        s = cv2.GaussianBlur(dm.sharpness_map(gray), (0, 0), 5)
        sharp.append(float(s.max()))
        res, _ = dm.focusedNozzleDetection(gray)
        if res is not None:
            centers.append(res[:2])
    ok = len(centers)
    jitter = float(np.linalg.norm(np.std(centers, axis=0))) if ok >= 2 else float("inf")
    return float(np.mean(sharp)), ok, jitter


def main():
    global Z_MIN, Z_MAX
    if len(sys.argv) == 3:
        Z_MIN, Z_MAX = sorted((float(sys.argv[1]), float(sys.argv[2])))
    print("Gama de Z: %.2f a %.2f (passo %.2f). Z_MIN tem de ser seguro." % (Z_MIN, Z_MAX, Z_STEP))
    if "xyz" not in homed_axes():
        print("A maquina nao esta homed (G28). Nada foi movido.")
        return
    dm = DM(log=lambda m: None, camera_url=SNAPSHOT_URL, cloud_url="")

    # Prato para o ponto mais afastado primeiro, so' depois a cabeca vai para cima da camara
    gcode("G90\nG0 Z%.2f F300\nM400" % Z_MAX)
    gcode("G0 X%.2f Y%.2f F1800\nM400" % (CAM_X, CAM_Y))

    n = int(round((Z_MAX - Z_MIN) / Z_STEP))
    zs = [round(Z_MAX - i * Z_STEP, 2) for i in range(n + 1)]   # do mais afastado para o mais perto
    print("   Z     nitidez  detecoes  jitter(px)")
    results = []
    for z in zs:
        s, ok, j = evaluate(dm, z)
        results.append((z, s, ok, j))
        print("  %5.2f  %7.2f    %d/%d     %6.2f" % (z, s, ok, FRAMES_PER_Z, j))

    good = [r for r in results if r[2] == FRAMES_PER_Z]
    if not good:
        gcode("G0 Z%.2f F300\nM400" % Z_MAX)
        print("\nO bico nao foi detetado em nenhum Z. Prato devolvido a Z%.2f." % Z_MAX)
        return
    best = max(good, key=lambda r: r[1])
    # margem: Zs com detecao total e nitidez >= 90% da melhor
    ok_z = [r[0] for r in good if r[1] >= 0.9 * best[1]]
    gcode("G0 Z%.2f F300\nM400" % best[0])
    print("\nMelhor: Z=%.2f (nitidez %.2f, jitter %.2f px). O prato ficou neste Z."
          % (best[0], best[1], best[3]))
    print("Margem com >=90%% da nitidez e detecao total: Z %.2f a %.2f" % (min(ok_z), max(ok_z)))
    if min(ok_z) <= Z_MIN + 1e-6:
        print("Aviso: a margem chega ao Z_MIN. O foco ideal pode estar abaixo de Z%.2f." % Z_MIN)
    if max(ok_z) >= Z_MAX - 1e-6:
        print("Aviso: a margem chega ao Z_MAX. O foco ideal pode estar acima de Z%.2f." % Z_MAX)


if __name__ == "__main__":
    main()
