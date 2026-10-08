#!/usr/bin/env python3
"""
Teste do detetor novo (nozzle_detector.py) contra o atual, na maquina.

Modo "frames" - repetibilidade com o bico parado:
  tira N imagens na posicao atual e corre os dois detetores em cada uma.
  Mostra: deteccoes, centro medio, desvio e maior afastamento (px).

Modo "grid" - precisao com deslocamentos conhecidos:
  move o bico numa grelha (5x5, +-0.4 mm por defeito), tira imagens em cada
  ponto e ajusta a relacao mm -> px. Mostra a escala (px/mm), a rotacao da
  camara e o ERRO de cada detetor em micrometros (quanto o centro detetado se
  afasta do sitio onde devia estar, sabendo quanto a cabeca se moveu).

Em ambos: liga o anel de LEDs e a exposicao indicados, e no fim repoe tudo
(anel, exposicao e posicao). Guarda imagens e resultados em
~/ktamv_dettest_<NOME>/ para analise.

Antes de correr: bico (T0 ou T1) centrado na camara, a altura de foco.
Uso:
  python3 ktamv_detector_test.py frames t0_frames
  python3 ktamv_detector_test.py grid   t0_grid
  opcoes: --led 1.0 --exposure 300 --frames 30 --step 0.2 --points 5
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
from ktamv_server_dm import Ktamv_Server_Detection_Manager as DM   # detetor atual
from nozzle_detector import NozzleDetector                          # detetor novo

MOONRAKER = "http://localhost:7125"
DEVICE = "/dev/v4l/by-path/platform-xhci-hcd.0-usbv2-0:1:1.0-video-index0"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
LED_NAME = "Led_Nozzle_Cam"
SETTLE_S = 0.8


def gcode(script):
    url = MOONRAKER + "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
    with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=60) as r:
        return json.load(r)


def query(objs):
    with urllib.request.urlopen(MOONRAKER + "/printer/objects/query?" + objs, timeout=5) as r:
        return json.load(r)["result"]["status"]


def v4l2_set(**ctrls):
    subprocess.run(["v4l2-ctl", "-d", DEVICE, "-c",
                    ",".join("%s=%s" % kv for kv in ctrls.items())], check=True)


def v4l2_get(name):
    out = subprocess.run(["v4l2-ctl", "-d", DEVICE, "--get-ctrl=" + name],
                         check=True, capture_output=True, text=True).stdout
    return int(out.split(":")[1].split()[0])


def grab():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        data = r.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    img = cv2.resize(img, (640, 480), interpolation=cv2.INTER_AREA)
    return data, cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


class Detectors:
    def __init__(self):
        self.old = DM(log=lambda m: None, camera_url=SNAPSHOT_URL, cloud_url="")
        self.new = NozzleDetector()

    def run(self, gray):
        o = self.old.focusedNozzleDetection(gray)[0]
        t = time.time()
        n = self.new.detect(gray)
        ms = (time.time() - t) * 1000
        old = (float(o[0]), float(o[1])) if o is not None else None
        new = n["center"] if (n and n["method"] == "line") else None
        return old, new, n, ms


def stats(points):
    if not points:
        return None
    p = np.array(points)
    m = p.mean(axis=0)
    d = np.hypot(*(p - m).T)
    return {"n": len(p), "mean": m.tolist(), "std": p.std(axis=0).tolist(), "max_dev": float(d.max())}


# ------------------------------------------------------------------ modos
def mode_frames(a, out, dets):
    rows, old_c, new_c, times = [], [], [], []
    print(" img   atual (x, y)          novo (x, y)           metodo  raio")
    for i in range(a.frames):
        data, gray = grab()
        with open(os.path.join(out, "frame_%03d.jpg" % i), "wb") as f:
            f.write(data)
        old, new, n, ms = dets.run(gray)
        times.append(ms)
        if old: old_c.append(old)
        if new: new_c.append(new)
        rows.append({"i": i, "old": old, "new": new, "new_method": n["method"] if n else None,
                     "new_radius": n["radius"] if n else None})
        print(" %3d   %-20s  %-20s  %-7s %s" % (
            i, "(%.2f, %.2f)" % old if old else "-", "(%.2f, %.2f)" % tuple(new) if new else "-",
            n["method"] if n else "-", "%.1f" % n["radius"] if n else "-"))
        time.sleep(0.2)
    so, sn = stats(old_c), stats(new_c)
    print("\nRESUMO (%d imagens, bico parado)" % a.frames)
    for name, s in (("atual", so), ("novo ", sn)):
        if s:
            print("  %s: detetado %2d/%d | centro (%.2f, %.2f) | desvio (%.3f, %.3f) px | maior afastamento %.2f px"
                  % (name, s["n"], a.frames, s["mean"][0], s["mean"][1], s["std"][0], s["std"][1], s["max_dev"]))
        else:
            print("  %s: nenhuma detecao" % name)
    print("  tempo do detetor novo: %.0f ms por imagem (media)" % np.mean(times))
    return {"rows": rows, "old": so, "new": sn, "new_ms": float(np.mean(times))}


def fit_affine(mm, px):
    """px = A @ mm + b (minimos quadrados). Devolve A, b, residuos em px."""
    mm, px = np.array(mm), np.array(px)
    X = np.c_[mm, np.ones(len(mm))]
    sol, *_ = np.linalg.lstsq(X, px, rcond=None)
    pred = X @ sol
    return sol[:2].T, sol[2], px - pred


def mode_grid(a, out, dets):
    k = (a.points - 1) // 2
    offsets = [(ix * a.step, iy * a.step) for iy in range(-k, k + 1) for ix in range(-k, k + 1)]
    per = []
    cur = (0.0, 0.0)
    print(" ponto (mm)        atual (media px)        novo (media px)")
    try:
        gcode("G91")
        for dx, dy in offsets:
            gcode("G1 X%.4f Y%.4f F600\nM400" % (dx - cur[0], dy - cur[1]))
            cur = (dx, dy)
            time.sleep(SETTLE_S)
            grab()
            olds, news = [], []
            for i in range(a.frames):
                data, gray = grab()
                if i == 0:
                    with open(os.path.join(out, "x%+.2f_y%+.2f.jpg" % (dx, dy)), "wb") as f:
                        f.write(data)
                old, new, n, ms = dets.run(gray)
                if old: olds.append(old)
                if new: news.append(new)
            so, sn = stats(olds), stats(news)
            per.append({"dx": dx, "dy": dy, "old": so, "new": sn})
            print(" (%+.2f, %+.2f)    %-22s  %-22s" % (
                dx, dy, "(%.2f, %.2f) %d/%d" % (so["mean"][0], so["mean"][1], so["n"], a.frames) if so else "-",
                "(%.2f, %.2f) %d/%d" % (sn["mean"][0], sn["mean"][1], sn["n"], a.frames) if sn else "-"))
    finally:
        gcode("G1 X%.4f Y%.4f F600\nG90\nM400" % (-cur[0], -cur[1]))

    result = {"points": per}
    print("\nRESULTADO (grelha %dx%d, passo %.2f mm)" % (a.points, a.points, a.step))
    for key, label in (("old", "atual"), ("new", "novo ")):
        ok = [p for p in per if p[key]]
        if len(ok) < 4:
            print("  %s: pontos detetados insuficientes (%d)" % (label, len(ok)))
            continue
        A, b, res = fit_affine([(p["dx"], p["dy"]) for p in ok], [p[key]["mean"] for p in ok])
        sx, sy = np.linalg.norm(A[:, 0]), np.linalg.norm(A[:, 1])
        rot = np.degrees(np.arctan2(A[1, 0], A[0, 0]))
        um_per_px = 1000.0 / ((sx + sy) / 2)
        err_um = np.hypot(*res.T) * um_per_px
        print("  %s: %d/%d pontos | escala X %.1f px/mm, Y %.1f px/mm | rotacao %.2f graus | erro medio %.1f um, maximo %.1f um"
              % (label, len(ok), len(per), sx, sy, rot, err_um.mean(), err_um.max()))
        result[key + "_fit"] = {"A": A.tolist(), "b": b.tolist(), "err_um": err_um.tolist(),
                                "scale_px_mm": [float(sx), float(sy)], "rotation_deg": float(rot)}
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["frames", "grid"])
    ap.add_argument("name")
    ap.add_argument("--led", type=float, default=1.0)
    ap.add_argument("--exposure", type=int, default=300)
    ap.add_argument("--frames", type=int, default=None)
    ap.add_argument("--step", type=float, default=0.2)
    ap.add_argument("--points", type=int, default=5)
    a = ap.parse_args()
    if a.frames is None:
        a.frames = 30 if a.mode == "frames" else 5

    st = query("toolhead=position,homed_axes&dual_carriage&neopixel%20" + LED_NAME)
    if "xyz" not in st["toolhead"]["homed_axes"]:
        print("A maquina nao esta homed. Nada foi feito.")
        return
    tool = 1 if st.get("dual_carriage", {}).get("carriage_1") == "PRIMARY" else 0
    led0 = (st.get("neopixel " + LED_NAME, {}).get("color_data") or [[0, 0, 0]])[0][:3]
    exp0 = v4l2_get("exposure_time_absolute")
    out = os.path.expanduser("~/ktamv_dettest_%s" % a.name)
    os.makedirs(out, exist_ok=True)
    meta = {"name": a.name, "mode": a.mode, "tool": tool, "led": a.led, "exposure": a.exposure,
            "position": st["toolhead"]["position"], "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    print("T%d | anel %.2f | exposicao %d | modo %s" % (tool, a.led, a.exposure, a.mode))
    dets = Detectors()
    try:
        gcode("SET_LED LED=%s RED=%.3f GREEN=%.3f BLUE=%.3f" % (LED_NAME, a.led, a.led, a.led))
        v4l2_set(auto_exposure=1, exposure_time_absolute=a.exposure)
        time.sleep(SETTLE_S)
        grab()
        meta["result"] = mode_frames(a, out, dets) if a.mode == "frames" else mode_grid(a, out, dets)
    finally:
        v4l2_set(exposure_time_absolute=exp0)
        gcode("SET_LED LED=%s RED=%.3f GREEN=%.3f BLUE=%.3f" % ((LED_NAME,) + tuple(led0)))
        with open(os.path.join(out, "results.json"), "w") as f:
            json.dump(meta, f, indent=1)
    print("Anel, exposicao e posicao repostos. Resultados em %s" % out)


if __name__ == "__main__":
    main()
