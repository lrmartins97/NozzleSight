#!/usr/bin/env python3
"""
Teste de iluminacao da camara do bico: intensidade do anel de LEDs x exposicao.

Para cada combinacao liga o anel (via Moonraker), ajusta a exposicao, tira
varias imagens e corre o detetor do kTAMV (o mesmo da calibracao). Mede:
  detecoes   em quantas imagens encontrou o bico (tem de ser todas)
  jitter     quanto o centro varia entre imagens (px; menor = melhor)
  centro     centro medio detetado (com o bico parado, deve ser igual em todas)
  raio       raio medio do circulo detetado (px)
  brilho     brilho medio da imagem (0-255)
  saturado   % de pixeis queimados na zona do bico

As exposicoes sao escolhidas para dar o MESMO brilho em cada intensidade
(intensidade x exposicao = nivel). Assim compara-se so o efeito da intensidade:
com LED forte e exposicao curta, a luz da sala pesa menos.

No fim repoe o anel e a exposicao como estavam. Guarda tudo em
~/ktamv_light_<NOME>/ (imagens + results.json) para analise.

Antes de correr:
  - T0 (ou T1) em cima da camara, a altura de foco (ex.: G0 X22 Y113 / G0 Z6.7)
  - as camaras das ferramentas no estado que queres testar (TOOL_CAMERAS_OFF)
Uso:
  python3 ktamv_light_test.py NOME
  python3 ktamv_light_test.py NOME --intensities 0.25 0.5 1.0 --levels 10 15 25 40 60
"""
import argparse, json, os, subprocess, sys, time, urllib.parse, urllib.request
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
DEVICE = "/dev/v4l/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0-video-index0"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
LED_NAME = "Led_Nozzle_Cam"
TOOL_CAM_PORTS = (8082, 8083)
FRAMES = 8          # imagens analisadas por combinacao
SAVE_FRAMES = 3     # destas, quantas ficam guardadas
SETTLE_S = 0.8
EXP_MIN, EXP_MAX = 3, 2047


def gcode(script):
    url = MOONRAKER + "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
    with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=30) as r:
        return json.load(r)


def query(objs):
    url = MOONRAKER + "/printer/objects/query?" + objs
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)["result"]["status"]


def set_led(level):
    gcode("SET_LED LED=%s RED=%.3f GREEN=%.3f BLUE=%.3f" % (LED_NAME, level, level, level))


def v4l2_set(**ctrls):
    arg = ",".join("%s=%s" % kv for kv in ctrls.items())
    subprocess.run(["v4l2-ctl", "-d", DEVICE, "-c", arg], check=True)


def v4l2_get(name):
    out = subprocess.run(["v4l2-ctl", "-d", DEVICE, "--get-ctrl=" + name],
                         check=True, capture_output=True, text=True).stdout
    return int(out.split(":")[1].split()[0])


def grab():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        data = r.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return data, cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA)


def tool_cams_running():
    return [p for p in TOOL_CAM_PORTS
            if subprocess.run(["pgrep", "-f", "ustreamer --host 127.0.0.1 --port %d " % p],
                              stdout=subprocess.DEVNULL).returncode == 0]


def measure(dm, out_dir, tag):
    centers, radii, bright, sat = [], [], [], []
    for i in range(FRAMES):
        data, img = grab()
        if i < SAVE_FRAMES:
            with open(os.path.join(out_dir, "%s_%d.jpg" % (tag, i)), "wb") as f:
                f.write(data)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        bright.append(float(gray.mean()))
        res, mask = dm.focusedNozzleDetection(gray)
        if mask is not None:
            roi = gray[mask > 0]
            sat.append(100.0 * np.count_nonzero(roi >= 250) / max(roi.size, 1))
        if res is not None:
            centers.append(res[:2]); radii.append(res[2])
    c = np.array(centers)
    return {
        "detections": len(centers), "frames": FRAMES,
        "jitter_px": float(np.linalg.norm(c.std(axis=0))) if len(c) >= 2 else None,
        "center": [float(v) for v in c.mean(axis=0)] if len(c) else None,
        "radius": float(np.median(radii)) if radii else None,
        "brightness": float(np.mean(bright)),
        "saturated_pct": float(np.mean(sat)) if sat else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--intensities", type=float, nargs="+", default=[0.25, 0.5, 1.0])
    ap.add_argument("--levels", type=float, nargs="+", default=[10, 15, 25, 40, 60],
                    help="intensidade x exposicao (brilho equivalente)")
    a = ap.parse_args()

    st = query("toolhead=position&dual_carriage&neopixel%20" + LED_NAME)
    tool = 1 if st.get("dual_carriage", {}).get("carriage_1") == "PRIMARY" else 0
    led0 = (st.get("neopixel " + LED_NAME, {}).get("color_data") or [[0, 0, 0]])[0][:3]
    exp0 = v4l2_get("exposure_time_absolute")
    out = os.path.expanduser("~/ktamv_light_%s" % a.name)
    os.makedirs(out, exist_ok=True)
    dm = DM(log=lambda m: None, camera_url=SNAPSHOT_URL, cloud_url="")
    meta = {"name": a.name, "tool": tool, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "position": st["toolhead"]["position"], "tool_cams_running": tool_cams_running(),
            "results": []}
    print("T%d | camaras das ferramentas a transmitir: %s" %
          (tool, meta["tool_cams_running"] or "nenhuma"))
    print(" LED  exposicao  detecoes  jitter(px)      centro       raio  brilho  saturado(%)")
    try:
        v4l2_set(auto_exposure=1)
        for inten in a.intensities:
            set_led(inten)
            for lvl in a.levels:
                exp = int(round(lvl / inten))
                if not (EXP_MIN <= exp <= EXP_MAX):
                    continue
                v4l2_set(exposure_time_absolute=exp)
                time.sleep(SETTLE_S)
                grab()                                   # descarta um frame
                r = measure(dm, out, "led%03d_exp%04d" % (round(inten * 100), exp))
                r.update(led=inten, exposure=exp, level=lvl)
                meta["results"].append(r)
                c = "(%.1f, %.1f)" % tuple(r["center"]) if r["center"] else "-"
                print(" %.2f  %6d      %d/%d     %6s  %16s  %5s  %6.1f   %6s" % (
                    inten, exp, r["detections"], FRAMES,
                    "%.2f" % r["jitter_px"] if r["jitter_px"] is not None else "-",
                    c, "%.1f" % r["radius"] if r["radius"] else "-",
                    r["brightness"],
                    "%.1f" % r["saturated_pct"] if r["saturated_pct"] is not None else "-"))
    finally:
        v4l2_set(exposure_time_absolute=exp0)
        gcode("SET_LED LED=%s RED=%.3f GREEN=%.3f BLUE=%.3f" % ((LED_NAME,) + tuple(led0)))
        with open(os.path.join(out, "results.json"), "w") as f:
            json.dump(meta, f, indent=1)

    cs = np.array([r["center"] for r in meta["results"] if r["center"] and r["detections"] == FRAMES])
    if len(cs):
        print("\nCentro em todas as combinacoes com deteccao total: variacao X %.2f px, Y %.2f px" %
              tuple(np.ptp(cs, axis=0)))
    print("Anel e exposicao repostos (exposicao %d). Resultados em %s" % (exp0, out))


if __name__ == "__main__":
    main()
