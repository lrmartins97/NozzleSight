#!/usr/bin/env python3
"""
Teste de repetibilidade da calibracao XY automatica (sem supervisao).

Corre CALIBRATE_IDEX_XY N vezes seguidas e regista, em cada corrida:
hora, duracao, sucesso ou erro (com a mensagem) e o offset X/Y do T1 gravado.
Uma corrida que falha nao para o teste: a maquina e' reposta (camaras e anel,
T0) e segue para a seguinte. Para ao fim de 5 falhas seguidas (algo esta
errado e nao vale a pena continuar).

No fim (ou se for interrompido) escreve o resumo: quantas correram bem e a
dispersao dos offsets (media, desvio, minimo, maximo, amplitude).

Ficheiros em ~/ktamv_repeat_<data>/:
  runs.csv     uma linha por corrida
  summary.txt  resumo final
  run.log      tudo o que o script escreveu
  detector.log o que o detetor fez em cada medicao (linha escolhida, posicoes)

Uso (a correr em segundo plano, continua mesmo que feches o terminal):
  nohup python3 ~/ktamv_repeat_test.py 100 > /dev/null 2>&1 &
  nohup python3 ~/ktamv_repeat_test.py 100 --home > /dev/null 2>&1 &
    (--home: faz G28 antes de cada calibracao, como no PRINT_START real)
Ver o progresso:
  tail -f ~/ktamv_repeat_*/run.log
Parar antes do fim:
  pkill -f ktamv_repeat_test.py
"""
import csv, json, os, statistics, sys, time, urllib.parse, urllib.request, urllib.error

MOONRAKER = "http://localhost:7125"
SNAPSHOT_URL = "http://localhost/webcam2/snapshot?max_delay=0"
CAL_COMMAND = "CALIBRATE_IDEX_XY"
PAUSE_BETWEEN_S = 20          # entre corridas (o crowsnest reinicia no fim de cada uma)
MAX_CONSECUTIVE_FAILS = 5
CMD_TIMEOUT_S = 900           # uma calibracao demora 1-3 min; margem larga
DETECTOR_LOG = os.path.expanduser("~/ktamv_detector.log")


class Runner:
    def __init__(self, n, home=False):
        self.n = n
        self.home = home
        stamp = time.strftime("%Y%m%d_%H%M")
        self.out = os.path.expanduser("~/ktamv_repeat_%s" % stamp)
        os.makedirs(self.out, exist_ok=True)
        self.logf = open(os.path.join(self.out, "run.log"), "a", buffering=1)
        self.rows = []
        # parte do registo do detetor que pertence a este teste
        self.det_log_start = os.path.getsize(DETECTOR_LOG) if os.path.exists(DETECTOR_LOG) else 0

    def log(self, msg):
        line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        self.logf.write(line + "\n")

    # --- Moonraker --------------------------------------------------------
    def gcode(self, script, timeout=CMD_TIMEOUT_S):
        """Devolve (ok, mensagem de erro)."""
        url = MOONRAKER + "/printer/gcode/script?" + urllib.parse.urlencode({"script": script})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="POST"), timeout=timeout) as r:
                json.load(r)
            return True, ""
        except urllib.error.HTTPError as e:
            try:
                msg = json.load(e)["error"]["message"]
            except Exception:
                msg = "HTTP %d" % e.code
            return False, msg
        except Exception as e:
            return False, str(e)

    def query(self, objs):
        with urllib.request.urlopen(MOONRAKER + "/printer/objects/query?" + objs, timeout=10) as r:
            return json.load(r)["result"]["status"]

    def camera_ok(self):
        try:
            with urllib.request.urlopen(SNAPSHOT_URL, timeout=5) as r:
                return len(r.read()) > 1000
        except Exception:
            return False

    def wait_ready(self):
        """Espera que o Klipper esteja pronto e a camara do bico responda."""
        for _ in range(60):
            try:
                st = self.query("webhooks")
                if st["webhooks"]["state"] == "ready" and self.camera_ok():
                    return True
            except Exception:
                pass
            time.sleep(5)
        return False

    def saved_offsets(self):
        v = self.query("save_variables")["save_variables"]["variables"]
        return v.get("tool_1_x_offset"), v.get("tool_1_y_offset"), v.get("tool_1_xy_calibrated_at")

    def recover(self):
        # Depois de uma falha: camaras e anel como no uso normal, T0 ativo.
        self.gcode("_CALIB_VISION_OFF", timeout=60)
        self.gcode("T0", timeout=120)

    # --- teste ------------------------------------------------------------
    def run(self):
        self.log("Inicio: %d corridas de %s%s. Resultados em %s"
                 % (self.n, CAL_COMMAND, " (com G28 antes de cada uma)" if self.home else "", self.out))
        fails_in_row = 0
        try:
            for i in range(1, self.n + 1):
                if not self.wait_ready():
                    self.log("Klipper ou camara sem resposta ha 5 minutos: teste parado.")
                    break
                _, _, before = self.saved_offsets()
                t0 = time.time()
                ok, err = self.gcode("G28") if self.home else (True, "")
                if ok:
                    ok, err = self.gcode(CAL_COMMAND)
                else:
                    err = "G28 falhou: " + err
                dur = time.time() - t0
                x, y, after = self.saved_offsets()
                if ok and after != before:
                    fails_in_row = 0
                    self.rows.append({"run": i, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": 1,
                                      "duration_s": round(dur, 1), "x": x, "y": y, "error": ""})
                    self.log("Corrida %3d/%d OK   em %5.1f s   X=%.4f  Y=%.4f" % (i, self.n, dur, x, y))
                else:
                    fails_in_row += 1
                    if ok:
                        err = "terminou sem gravar offset novo"
                    self.rows.append({"run": i, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": 0,
                                      "duration_s": round(dur, 1), "x": "", "y": "", "error": err})
                    self.log("Corrida %3d/%d FALHOU em %5.1f s: %s" % (i, self.n, dur, err))
                    self.recover()
                    if fails_in_row >= MAX_CONSECUTIVE_FAILS:
                        self.log("%d falhas seguidas: teste parado." % fails_in_row)
                        break
                self.write_csv()
                time.sleep(PAUSE_BETWEEN_S)
        except KeyboardInterrupt:
            self.log("Interrompido.")
        finally:
            self.write_csv()
            self.write_summary()
            self.copy_detector_log()
            self.recover()
            self.log("Fim. Maquina reposta (camaras ligadas, anel apagado, T0).")

    def copy_detector_log(self):
        try:
            with open(DETECTOR_LOG) as f:
                f.seek(self.det_log_start)
                data = f.read()
            with open(os.path.join(self.out, "detector.log"), "w") as f:
                f.write(data)
        except Exception as e:
            self.log("Registo do detetor nao copiado: %s" % e)

    def write_csv(self):
        with open(os.path.join(self.out, "runs.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["run", "time", "ok", "duration_s", "x", "y", "error"])
            w.writeheader()
            w.writerows(self.rows)

    def write_summary(self):
        ok = [r for r in self.rows if r["ok"]]
        lines = ["Corridas: %d | bem: %d | falhadas: %d" % (len(self.rows), len(ok), len(self.rows) - len(ok))]
        if len(ok) >= 2:
            for axis in ("x", "y"):
                vals = [float(r[axis]) for r in ok]
                lines.append("Offset %s: media %.4f mm | desvio %.1f um | min %.4f | max %.4f | amplitude %.1f um"
                             % (axis.upper(), statistics.mean(vals), statistics.pstdev(vals) * 1000,
                                min(vals), max(vals), (max(vals) - min(vals)) * 1000))
            durs = [r["duration_s"] for r in ok]
            lines.append("Duracao por corrida: media %.0f s, maxima %.0f s" % (statistics.mean(durs), max(durs)))
        errs = {}
        for r in self.rows:
            if not r["ok"]:
                errs[r["error"]] = errs.get(r["error"], 0) + 1
        for e, c in sorted(errs.items(), key=lambda kv: -kv[1]):
            lines.append("Erro (%dx): %s" % (c, e))
        with open(os.path.join(self.out, "summary.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        for l in lines:
            self.log(l)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = int(args[0]) if args else 100
    Runner(n, home="--home" in sys.argv).run()
