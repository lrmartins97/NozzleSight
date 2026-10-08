# idex_camera_focus.py - altura de foco da camara do bico (IDEX, kTAMV)
#
# A camara do bico esta no prato e tem foco fixo: a distancia da ponta a camara
# (o Z do prato) e' o foco. Este modulo mede o Z em que a ponta do T0 fica mais
# nitida e grava-o no variables.cfg (camera_focus_z). As calibracoes X/Y
# (automatica e manual) usam esse valor; sem valor gravado, usam o camera_z do
# idex.cfg.
#
# Comando (chamado pela macro CALIBRATE_IDEX_CAMERA_FOCUS, que prepara a
# maquina: home, limpeza, luz, T0 em cima da camara):
#   IDEX_CAMERA_FOCUS_SCAN [ZMIN=<z> ZMAX=<z>] [SAVE=0|1]
#   Sem ZMIN/ZMAX percorre range mm para cada lado do foco atual.
#
# Como mede:
#   1. Com o T0 em cima da camara, pede ao servidor kTAMV a posicao da ponta
#      e fixa a zona da imagem onde a nitidez vai ser medida.
#   2. Percorre o Z do mais afastado para o mais proximo (RANGE mm para cada
#      lado do foco atual, nunca abaixo de z_min), e em cada altura pede ao
#      servidor a nitidez da ponta e se ela e' detetada.
#   3. Encontra o pico de nitidez e a zona em que a nitidez fica >= 90% do pico
#      (com a ponta sempre detetada). O foco gravado e' o CENTRO dessa zona:
#      mais estavel do que o pico, que varia com o ruido.
#   4. So' grava se o resultado for fiavel: pico dentro do intervalo (nao no limite),
#      zona com pelo menos min_margin mm e pontos suficientes com a ponta vista.
#
# O Z gravado e' fisico (sem babystep): as macros convertem-no para a posicao
# G-code da ferramenta ativa.
#
# Configuracao:
#   [idex_camera_focus]
#   #server_url: http://localhost:8085
#   #range: 1.0          # mm para cada lado do foco atual
#   #step: 0.05          # mm entre alturas
#   #frames: 3           # imagens por altura
#   #settle_time: 0.5    # s de espera depois de cada movimento
#   #z_min: 3.0          # Z fisico minimo (seguranca: prato nunca mais perto)
#   #min_margin: 0.15    # mm minimos da zona nitida para aceitar
#   #z_speed: 5          # mm/s
#
# Coloca este ficheiro em: ~/klipper/klippy/extras/idex_camera_focus.py
import json, logging, threading, time, urllib.parse, urllib.request

VAR_Z = "camera_focus_z"
VAR_MARGIN = "camera_focus_margin"
VAR_AT = "camera_focus_calibrated_at"


def analyze(points, frames, min_margin, min_valid=5, band=0.9):
    """points: lista (z, nitidez, detetadas) na ordem da varredura.
    Devolve dict com ok, focus, peak, lo, hi, reason. Funcao pura (testavel)."""
    need = max(1, int(round(frames * 2.0 / 3.0)))
    pts = sorted(points, key=lambda p: p[0])
    valid = [p for p in pts if p[2] >= need]
    if len(valid) < min_valid:
        return {'ok': False, 'reason': "a ponta so' foi detetada em %d alturas"
                " (minimo %d). Confirma que o T0 esta em cima da camara e a"
                " ponta esta limpa." % (len(valid), min_valid)}
    zs = [p[0] for p in pts]
    i_peak = max(range(len(pts)), key=lambda i: pts[i][1] if pts[i][2] >= need else -1)
    peak_s = pts[i_peak][1]
    if i_peak == 0 or i_peak == len(pts) - 1:
        return {'ok': False, 'reason': "o pico de nitidez esta no limite do intervalo"
                " (Z=%.2f): o foco pode estar fora dele. Repete com um"
                " intervalo maior (Z_MIN/Z_MAX)." % pts[i_peak][0]}
    # pico sub-passo por parabola nos vizinhos
    (z0, s0, _), (z1, s1, _), (z2, s2, _) = pts[i_peak - 1], pts[i_peak], pts[i_peak + 1]
    den = s0 - 2 * s1 + s2
    peak_z = z1 + (0.5 * (s0 - s2) / den * (z2 - z1) if den < 0 else 0.0)
    # zona continua a volta do pico com nitidez >= band * pico e ponta detetada
    lo = hi = i_peak
    while lo - 1 >= 0 and pts[lo - 1][2] >= need and pts[lo - 1][1] >= band * peak_s:
        lo -= 1
    while hi + 1 < len(pts) and pts[hi + 1][2] >= need and pts[hi + 1][1] >= band * peak_s:
        hi += 1
    if lo == 0 or hi == len(pts) - 1:
        return {'ok': False, 'reason': "a zona nitida chega ao limite do intervalo"
                " (Z %.2f a %.2f). Repete com um intervalo maior (Z_MIN/Z_MAX)." % (pts[lo][0], pts[hi][0])}
    # limites da zona interpolados entre alturas (onde a nitidez cruza band*pico)
    thr = band * peak_s

    def cross(ia, ib):
        (za, sa, _), (zb, sb, _) = pts[ia], pts[ib]
        return za + (thr - sa) / (sb - sa) * (zb - za) if sb != sa else za
    z_lo = cross(lo - 1, lo) if pts[lo - 1][2] >= need else pts[lo][0]
    z_hi = cross(hi + 1, hi) if pts[hi + 1][2] >= need else pts[hi][0]
    if z_hi - z_lo < min_margin - 1e-9:
        return {'ok': False, 'reason': "a zona nitida e' muito estreita (%.2f mm,"
                " minimo %.2f). A imagem da ponta pode estar fraca: limpa a ponta"
                " e confirma a luz." % (z_hi - z_lo, min_margin)}
    return {'ok': True, 'focus': float((z_lo + z_hi) / 2.0), 'peak': float(peak_z),
            'lo': float(z_lo), 'hi': float(z_hi), 'peak_sharpness': float(peak_s)}


class IdexCameraFocus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.server_url = config.get('server_url', 'http://localhost:8085').rstrip('/')
        self.range = config.getfloat('range', 1.0, above=0.1, maxval=3.0)
        self.step = config.getfloat('step', 0.05, minval=0.01, maxval=0.5)
        self.frames = config.getint('frames', 3, minval=1, maxval=10)
        self.settle = config.getfloat('settle_time', 0.5, minval=0.)
        self.z_min = config.getfloat('z_min', 3.0)
        self.min_margin = config.getfloat('min_margin', 0.15, minval=0.)
        self.z_speed = config.getfloat('z_speed', 5., above=0.)
        self.printer.load_object(config, 'save_variables')
        self.gcode.register_command(
            'IDEX_CAMERA_FOCUS_SCAN', self.cmd_IDEX_CAMERA_FOCUS_SCAN,
            desc="Mede a altura de foco da camara do bico (usar via"
                 " CALIBRATE_IDEX_CAMERA_FOCUS)")
        self.gcode.register_command(
            'IDEX_KTAMV_MATRIX_CHECK', self.cmd_IDEX_KTAMV_MATRIX_CHECK,
            desc="Pergunta ao servidor kTAMV se tem a calibracao da camara")
        # Resultado da ultima verificacao (lido pelas macros como
        # printer.idex_camera_focus.matrix_ok)
        self.matrix_ok = False
        self.printer.register_event_handler('klippy:ready', self._restore_ktamv_mpp)

    def _restore_ktamv_mpp(self):
        # O kTAMV guarda o valor mm/pixel so' em memoria: repoe o ultimo valor
        # gravado (camera_mpp), para nao ser preciso recalibrar a camara a cada
        # arranque do Klipper. A tabela do servidor e' verificada antes de cada
        # calibracao X/Y (IDEX_KTAMV_MATRIX_CHECK); se faltar, a camara e'
        # recalibrada e os dois valores voltam a ficar certos.
        try:
            kt = self.printer.lookup_object('ktamv', None)
            mpp = self._vars().get('camera_mpp')
            if kt is None or mpp is None:
                return
            if getattr(kt, 'mpp', None) is None and float(mpp) > 0:
                kt.mpp = float(mpp)
                kt.is_calibrated = True
                logging.info("idex_camera_focus: mm/pixel do kTAMV reposto (%.5f)", float(mpp))
        except Exception:
            logging.exception("idex_camera_focus: erro a repor o mm/pixel do kTAMV")

    # --- utilitarios -------------------------------------------------------
    def _http_json(self, path, timeout=20.):
        """Pedido ao servidor kTAMV numa thread (o Klipper nao fica bloqueado)."""
        result = {}

        def worker():
            try:
                with urllib.request.urlopen(self.server_url + path, timeout=timeout) as r:
                    result['data'] = json.load(r)
            except Exception as e:
                result['error'] = str(e)
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        eventtime = self.reactor.monotonic()
        end = eventtime + timeout + 2.
        while th.is_alive() and eventtime < end:
            eventtime = self.reactor.pause(eventtime + 0.05)
        if 'data' not in result:
            raise self.gcode.error("Sem resposta do servidor kTAMV (%s): %s"
                                   % (self.server_url, result.get('error', 'tempo esgotado')))
        if 'error' in result['data']:
            raise self.gcode.error("Servidor kTAMV: %s" % result['data']['error'])
        return result['data']

    def _score(self, roi=None):
        q = {'frames': self.frames}
        if roi is not None:
            q.update(cx="%.1f" % roi[0], cy="%.1f" % roi[1], r="%.1f" % roi[2])
        return self._http_json("/getFocusScore?" + urllib.parse.urlencode(q))

    def _move_z_raw(self, z_raw):
        gm = self.printer.lookup_object('gcode_move')
        org_z = gm.get_status(self.reactor.monotonic())['homing_origin'].z
        self.gcode.run_script_from_command(
            "G1 Z%.4f F%d" % (z_raw - org_z, int(self.z_speed * 60)))
        self.printer.lookup_object('toolhead').wait_moves()
        self.reactor.pause(self.reactor.monotonic() + self.settle)

    def _vars(self):
        return self.printer.lookup_object('save_variables').allVariables

    def _save_var(self, name, value):
        # Sem passar pelo parser de G-code (que nao lida bem com espacos e aspas)
        sv = self.printer.lookup_object('save_variables')
        gc = self.gcode.create_gcode_command(
            "SAVE_VARIABLE", "SAVE_VARIABLE", {'VARIABLE': name, 'VALUE': repr(value)})
        sv.cmd_SAVE_VARIABLE(gc)

    def _current_focus(self):
        sv = self._vars()
        if VAR_Z in sv:
            return float(sv[VAR_Z]), "gravado em %s" % sv.get(VAR_AT, 'data desconhecida')
        m = self.printer.lookup_object('gcode_macro _idex_vars', None)
        if m is not None and 'camera_z' in m.variables:
            return float(m.variables['camera_z']), "camera_z do idex.cfg"
        raise self.gcode.error("Sem altura de foco de partida (camera_z em _idex_vars).")

    # --- calibracao da camara no servidor ------------------------------------
    def cmd_IDEX_KTAMV_MATRIX_CHECK(self, gcmd):
        try:
            data = self._http_json("/hasMatrix", timeout=5.)
            self.matrix_ok = bool(data.get('ok'))
        except Exception as e:
            # Servidor sem este pedido (versao antiga) ou em baixo: por
            # seguranca, a calibracao X/Y recalibra a camara
            self.matrix_ok = False
            gcmd.respond_info("Aviso: %s" % (str(e),))
        if not self.matrix_ok:
            gcmd.respond_info("O servidor kTAMV nao tem a calibracao da camara:"
                              " vai ser feita agora.")

    def get_status(self, eventtime):
        return {'matrix_ok': self.matrix_ok}

    # --- comando -------------------------------------------------------------
    def _vision_off(self):
        # A macro que chamou este comando ja nao chega ao fim quando ha erro:
        # repoe aqui a luz e as camaras das ferramentas
        try:
            self.gcode.run_script_from_command("_CALIB_VISION_OFF")
        except Exception:
            logging.exception("idex_camera_focus: erro a repor a luz")

    def cmd_IDEX_CAMERA_FOCUS_SCAN(self, gcmd):
        save = gcmd.get_int('SAVE', 1)
        eventtime = self.reactor.monotonic()
        toolhead = self.printer.lookup_object('toolhead')
        th_status = toolhead.get_status(eventtime)
        if 'z' not in th_status['homed_axes']:
            self._vision_off()
            raise gcmd.error("A maquina nao esta homed.")
        dc = self.printer.lookup_object('dual_carriage', None)
        if dc is not None and dc.get_status(eventtime).get('carriage_0') != 'PRIMARY':
            self._vision_off()
            raise gcmd.error("O foco mede-se com o T0 ativo.")
        z0, z0_src = self._current_focus()
        axis_max = th_status['axis_maximum'].z - 0.1
        zmin_p = gcmd.get_float('ZMIN', None)
        zmax_p = gcmd.get_float('ZMAX', None)
        if (zmin_p is None) != (zmax_p is None):
            self._vision_off()
            raise gcmd.error("Indica os dois limites (Z_MIN e Z_MAX), ou nenhum.")
        if zmin_p is not None:
            z_bottom, z_top = min(zmin_p, zmax_p), max(zmin_p, zmax_p)
            if z_bottom < self.z_min - 1e-9:
                self._vision_off()
                raise gcmd.error("Z_MIN=%.2f esta abaixo do limite de seguranca (z_min=%.2f"
                                 " em [idex_camera_focus]). Nada foi movido."
                                 % (z_bottom, self.z_min))
            if z_top > axis_max:
                self._vision_off()
                raise gcmd.error("Z_MAX=%.2f esta acima do curso do eixo Z (%.2f)."
                                 % (z_top, axis_max))
        else:
            z_top = min(z0 + self.range, axis_max)
            z_bottom = max(z0 - self.range, self.z_min)
        if z_top - z_bottom < 4 * self.step:
            self._vision_off()
            raise gcmd.error("Intervalo de Z demasiado curto (%.2f a %.2f): tem de ter"
                             " pelo menos %.2f mm." % (z_bottom, z_top, 4 * self.step))
        # O foco atual e' usado para ver a ponta antes de comecar: se estiver
        # fora do intervalo, usa o meio do intervalo
        if not (z_bottom <= z0 <= z_top):
            z0_view = (z_bottom + z_top) / 2.0
        else:
            z0_view = z0
        n = int(round((z_top - z_bottom) / self.step))
        zs = [z_top - i * self.step for i in range(n + 1)]

        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=_idex_focus\nG90")
        try:
            res = self._scan(gcmd, z0_view, zs)
        except Exception:
            # Em caso de erro, a luz e as camaras voltam ao normal (a macro que
            # chamou este comando ja nao chega ao fim)
            self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=_idex_focus MOVE=0")
            self._vision_off()
            raise
        self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=_idex_focus MOVE=0")
        self._report(gcmd, res, z0, z0_src, save)

    def _scan(self, gcmd, z0, zs):
        # Zona da imagem onde se mede: a ponta, vista no foco atual
        self._move_z_raw(z0)
        first = self._score()
        roi = None
        if first.get('center') and first.get('radius'):
            roi = (first['center'][0], first['center'][1], max(40., 1.6 * first['radius']))
        else:
            gcmd.respond_info("Aviso: a ponta nao foi detetada no foco atual; a"
                              " nitidez e' medida no centro da imagem.")
        # Varredura: do mais afastado (Z maior) para o mais proximo
        self._move_z_raw(zs[0])
        points = []
        for z in zs:
            self._move_z_raw(z)
            r = self._score(roi)
            points.append((z, float(r['sharpness']), int(r['detected'])))
        res = analyze(points, self.frames, self.min_margin)
        need = max(1, int(round(self.frames * 2.0 / 3.0)))
        table = "  ".join("%.2f:%.0f%s" % (z, sh, "" if d >= need else "*")
                          for z, sh, d in points)
        gcmd.respond_info("Nitidez por altura (Z:nitidez, * = ponta nao detetada):\n" + table)
        if not res['ok']:
            self._move_z_raw(z0)
            reason = res['reason']
            if zs[-1] <= self.z_min + 1e-6:
                reason += (" Nota: o intervalo esta limitado em baixo pelo z_min"
                           " (Z=%.2f), por seguranca." % self.z_min)
            raise gcmd.error("Foco NAO gravado: " + reason)
        # Deixa o prato no novo foco (a imagem fica nitida para confirmar)
        self._move_z_raw(res['focus'])
        return res

    def _report(self, gcmd, res, z0, z0_src, save):
        msg = ("Foco da camara: Z=%.3f (zona nitida %.2f a %.2f, pico %.3f)"
               % (res['focus'], res['lo'], res['hi'], res['peak']))
        msg += "\nAnterior (%s): Z=%.3f\nDiferenca: %+.3f mm" % (z0_src, z0, res['focus'] - z0)
        if save:
            self._save_var(VAR_Z, round(res['focus'], 3))
            self._save_var(VAR_MARGIN, "%.2f-%.2f" % (res['lo'], res['hi']))
            self._save_var(VAR_AT, time.strftime("%Y-%m-%d %H:%M:%S"))
            msg = "Gravado. " + msg
        else:
            msg = "Simulacao (nada gravado). " + msg
        gcmd.respond_info(msg)


def load_config(config):
    return IdexCameraFocus(config)
