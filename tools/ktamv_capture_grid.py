#!/usr/bin/env python3
"""
Captura em grelha para afinar e VALIDAR o detetor da camara do bico.

Move o bico ativo em passos conhecidos (grelha de 3x3, por defeito +-0.3 mm)
a volta da posicao atual e guarda varias imagens cruas em cada ponto. Como os
deslocamentos sao conhecidos, da para medir a precisao real do detetor sem
depender de marcas feitas a mao: o centro detetado tem de se mover exatamente
o mesmo que a cabeca.

Antes de correr:
  - bico da ferramenta a testar (T0 ou T1) centrado na camara, a altura de foco
    (ex.: T0 / G0 Z8.2 / G0 X22 Y113), com a iluminacao que queres testar
Uso:
  python3 ktamv_capture_grid.py NOME
  (NOME identifica o ensaio, ex.: t0_leds_on  -> pasta ~/ktamv_grid_t0_leds_on/)
"""
import json, os, sys, time, urllib.request, urllib.parse

MOONRAKER = "http://localhost:7125"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
STEP_MM = 0.3            # passo da grelha (mm)
FRAMES_PER_POINT = 3
SETTLE_S = 0.8
MOVE_SPEED = 600         # mm/min


def gcode(script):
    url = MOONRAKER + "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
    with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=60) as r:
        return json.load(r)


def query(obj):
    url = MOONRAKER + "/printer/objects/query?" + obj
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.load(r)["result"]["status"]


def grab():
    with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
        return r.read()


def main():
    if len(sys.argv) < 2:
        print("Uso: python3 ktamv_capture_grid.py NOME   (ex.: t0_leds_on)")
        return
    name = sys.argv[1]
    st = query("toolhead=homed_axes,position&dual_carriage")
    if "xyz" not in st["toolhead"]["homed_axes"]:
        print("A maquina nao esta homed. Nada foi movido.")
        return
    tool = 1 if st.get("dual_carriage", {}).get("carriage_1") == "PRIMARY" else 0
    out = os.path.expanduser("~/ktamv_grid_%s" % name)
    os.makedirs(out, exist_ok=True)

    meta = {"name": name, "tool": tool, "step_mm": STEP_MM,
            "start_position": st["toolhead"]["position"], "points": []}
    offsets = [(dx, dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
    print("Ferramenta ativa: T%d. Grelha 3x3 de %.2f mm, %d imagens por ponto." % (tool, STEP_MM, FRAMES_PER_POINT))
    cur = (0, 0)
    try:
        gcode("G91")
        for dx, dy in offsets:
            mx, my = (dx - cur[0]) * STEP_MM, (dy - cur[1]) * STEP_MM
            gcode("G1 X%.3f Y%.3f F%d\nM400" % (mx, my, MOVE_SPEED))
            cur = (dx, dy)
            time.sleep(SETTLE_S)
            grab()                                    # descarta um frame
            files = []
            for i in range(FRAMES_PER_POINT):
                fn = "x%+.2f_y%+.2f_%d.jpg" % (dx * STEP_MM, dy * STEP_MM, i)
                with open(os.path.join(out, fn), "wb") as f:
                    f.write(grab())
                files.append(fn)
            meta["points"].append({"dx_mm": dx * STEP_MM, "dy_mm": dy * STEP_MM, "files": files})
            print("  ponto (%+.2f, %+.2f) mm: %d imagens" % (dx * STEP_MM, dy * STEP_MM, len(files)))
    finally:
        # volta sempre a posicao de partida
        gcode("G1 X%.3f Y%.3f F%d\nG90\nM400" % (-cur[0] * STEP_MM, -cur[1] * STEP_MM, MOVE_SPEED))
    with open(os.path.join(out, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    print("Feito: %s (%d imagens). A cabeca voltou a posicao de partida." %
          (out, sum(len(p["files"]) for p in meta["points"])))


if __name__ == "__main__":
    main()
