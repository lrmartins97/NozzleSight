# Calibracao do offset Z entre os bicos T0 e T1 (IDEX) usando o endstop Z
# fisico que ja e acionado pelo proprio bico durante o homing.
#
# Metodos disponiveis:
#   1) SWITCH (automatico, rapido) - sonda ambos os bicos no switch fixo e
#      calcula tool_1_z_offset = z1 - z0. Tem uma camada de SANIDADE baseada
#      na dispersao entre amostras (deteta toque inconsistente por sujidade,
#      folga, etc). O nivel de rigor e' configuravel (1/2/3).
#
#   2) PAPEL (manual, recurso) - teste do papel com AMBOS os bicos sobre o
#      prato, em bloco atomico (papel T0 -> papel T1). Como mede as duas
#      cabecas frescas e subtrai, a base do T0 cancela-se: o resultado sai
#      correto mesmo com a base desatualizada, e nao precisa de mexer na
#      position_endstop (logo, sem SAVE_CONFIG/restart a meio).
#
# Estado da calibracao entre ferramentas (fonte unica de verdade):
#   Este modulo e' tambem o dono do ESTADO da calibracao do T1 em relacao ao T0
#   (offset Z, offset X/Y, se sao validos e quando foram medidos). A ordem certa
#   e' XY (provisorio) -> Z -> XY (final):
#     - o Z pelo switch precisa de um XY para os dois bicos tocarem no mesmo
#       ponto do switch;
#     - o XY final precisa do Z para os dois bicos estarem a mesma distancia da
#       camara (foco e paralaxe).
#   Comandos de estado:
#     IDEX_SAVE_XY_OFFSET      -> unica forma de gravar o offset X/Y (valida o Z)
#     IDEX_CHECK_XY_ALLOWED    -> barreira de entrada das calibracoes XY
#     IDEX_CALIBRATION_STATUS  -> mostra offsets, estado e datas
#     IDEX_CHECK_DUAL_READY    -> usado pelo PRINT_START com as duas ferramentas
#     PRINTCORE_CHANGED        -> invalida a calibracao (para o futuro sensor)
#
# Coloca este ficheiro em: ~/klipper/klippy/extras/idex_z_offset.py
# e reinicia o servico do Klipper (RESTART), nao apenas FIRMWARE_RESTART.

import logging, re, time
from . import manual_probe
from mcu import MCU_endstop


class EndstopWrapper:
    # Expoe os metodos que o probing_move precisa, a partir do endstop ja
    # existente do eixo Z. (Mesmo padrao do plugin klipper_z_calibration.)
    def __init__(self, endstop):
        self.mcu_endstop = endstop
        self.get_mcu = endstop.get_mcu
        self.add_stepper = endstop.add_stepper
        self.get_steppers = endstop.get_steppers
        self.home_start = endstop.home_start
        self.home_wait = endstop.home_wait
        self.query_endstop = endstop.query_endstop


class IdexZOffset:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.config = config

        # Ponto X,Y onde o bico toca no switch fixo (o mesmo do safe_z_home).
        sx, sy = config.get('switch_position').split(',')
        self.switch_x = float(sx)
        self.switch_y = float(sy)

        self.probe_speed = config.getfloat('probe_speed', 5.0, above=0.)
        self.lift_speed = config.getfloat('lift_speed', 10.0, above=0.)
        self.sample_retract = config.getfloat('sample_retract_dist', 2.0,
                                               above=0.)
        self.samples = config.getint('samples', 3, minval=1)
        # Piso de seguranca para a sondagem: o switch dispara perto de
        # position_endstop (~1.3mm); este valor limita o quanto o bico desce
        # caso o switch falhe. Mantem-se pequeno de proposito.
        self.probe_z_min = config.getfloat('probe_z_min', -2.0)
        self.safe_z = config.getfloat('safe_z', 10.0, above=0.)

        # --- SANIDADE ---
        # Nivel 1 = debug/bancada: calcula, reporta tudo (inclui o que os
        #           niveis 2/3 fariam), mas GRAVA SEMPRE.
        # Nivel 2 = equilibrado: se a dispersao passar do limite, NAO grava,
        #           mantem o valor antigo, avisa.
        # Nivel 3 = estrito: se passar, BLOQUEIA e encaminha para o papel.
        self.sanity_level = config.getint('sanity_level', 1, minval=1, maxval=3)
        self.sanity_dispersion_max = config.getfloat(
            'sanity_dispersion_max', 0.05, above=0.)

        # --- PAPEL ---
        self.paper_bed_x = config.getfloat('paper_bed_x', 50.0)
        self.paper_bed_y = config.getfloat('paper_bed_y', 70.0)

        # Nomes das variaveis em variables.cfg.
        self.x_var = config.get('x_offset_var', 'tool_1_x_offset')
        self.y_var = config.get('y_offset_var', 'tool_1_y_offset')
        self.z_var = config.get('z_offset_var', 'tool_1_z_offset')
        # Estado e datas da calibracao (gravados em variables.cfg).
        self.z_state_var = config.get('z_state_var', 'tool_1_z_state')
        self.xy_state_var = config.get('xy_state_var', 'tool_1_xy_state')
        self.z_time_var = config.get('z_time_var', 'tool_1_z_calibrated_at')
        self.xy_time_var = config.get('xy_time_var', 'tool_1_xy_calibrated_at')

        self.z_endstop = None
        self.last_z = 0.0

        # Estado do bloco do papel (altura do T0, entre os dois passos).
        self._paper_z_t0 = 0.0

        self.query_endstops = self.printer.load_object(config,
                                                       'query_endstops')
        self.printer.register_event_handler('klippy:connect',
                                             self._handle_connect)
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command(
            'CALIBRATE_IDEX_Z_OFFSET', self.cmd_CALIBRATE_IDEX_Z_OFFSET,
            desc="Mede a diferenca T1-T0 no switch (automatico) e grava, com"
                 " verificacao de sanidade")
        self.gcode.register_command(
            'MEASURE_ACTIVE_NOZZLE_Z', self.cmd_MEASURE_ACTIVE_NOZZLE_Z,
            desc="Sonda o bico ativo no switch e reporta a altura de disparo"
                 " (diagnostico)")
        self.gcode.register_command(
            'CALIBRATE_IDEX_Z_PAPER', self.cmd_CALIBRATE_IDEX_Z_PAPER,
            desc="Recurso manual: teste do papel T0->T1 encadeado, um so"
                 " comando (dois ACCEPT, um por bico)")
        self.gcode.register_command(
            'IDEX_SAVE_XY_OFFSET', self.cmd_IDEX_SAVE_XY_OFFSET,
            desc="Grava o offset X/Y do T1 (STATE=FINAL exige offset Z valido)")
        self.gcode.register_command(
            'IDEX_CHECK_XY_ALLOWED', self.cmd_IDEX_CHECK_XY_ALLOWED,
            desc="Verifica se uma calibracao X/Y pode comecar")
        self.gcode.register_command(
            'IDEX_CALIBRATION_STATUS', self.cmd_IDEX_CALIBRATION_STATUS,
            desc="Mostra os offsets do T1, o estado e as datas de calibracao")
        self.gcode.register_command(
            'IDEX_CHECK_DUAL_READY', self.cmd_IDEX_CHECK_DUAL_READY,
            desc="Recusa uma impressao com as duas ferramentas sem calibracao"
                 " entre ferramentas valida e final")
        self.gcode.register_command(
            'PRINTCORE_CHANGED', self.cmd_PRINTCORE_CHANGED,
            desc="Marca a calibracao entre ferramentas como invalida apos"
                 " troca de printcore")

    def _handle_connect(self):
        # Vai buscar o endstop ja criado por [stepper_z] (nome 'z').
        for endstop, name in self.query_endstops.endstops:
            if name in ('z', 'stepper_z'):
                if not isinstance(endstop, MCU_endstop):
                    raise self.printer.config_error(
                        "idex_z_offset: o endstop Z e virtual"
                        " (probe:z_virtual_endstop). Este modulo precisa do"
                        " endstop fisico cru em [stepper_z].")
                self.z_endstop = EndstopWrapper(endstop)
                break
        if self.z_endstop is None:
            raise self.printer.config_error(
                "idex_z_offset: nao encontrei o endstop do eixo Z.")

    def get_status(self, eventtime):
        return {'last_z': self.last_z,
                'z_valid': self._z_is_valid(),
                'xy_state': self._xy_state()}

    def _check_homed(self):
        toolhead = self.printer.lookup_object('toolhead')
        curtime = self.printer.get_reactor().monotonic()
        homed = toolhead.get_status(curtime)['homed_axes']
        return 'x' in homed and 'y' in homed and 'z' in homed

    def _require_xy_offset(self, gcmd):
        sv = self.printer.lookup_object('save_variables').allVariables
        if self.x_var not in sv or self.y_var not in sv:
            raise gcmd.error(
                "O metodo do switch precisa de um offset X/Y do T1 para os dois"
                " bicos tocarem no mesmo ponto do switch. Usa"
                " CALIBRATE_IDEX, ou o metodo manual (CALIBRATE_IDEX_Z MANUAL=1),"
                " que nao precisa de offset X/Y.")
        if self._xy_state() == 'invalid':
            gcmd.respond_info(
                "Aviso: o offset X/Y do T1 foi invalidado (troca de printcore)."
                " Se o T1 nao tocar no switch, usa CALIBRATE_IDEX.")

    # ------------------------------------------------------------------ #
    # Estado da calibracao                                               #
    # ------------------------------------------------------------------ #
    def _vars(self):
        return self.printer.lookup_object('save_variables').allVariables

    def _save_var(self, name, value):
        # Grava qualquer valor Python (incluindo texto) sem passar pelo parser
        # de G-code, que nao lida bem com espacos e aspas.
        sv = self.printer.lookup_object('save_variables')
        gc = self.gcode.create_gcode_command(
            "SAVE_VARIABLE", "SAVE_VARIABLE",
            {'VARIABLE': name, 'VALUE': repr(value)})
        sv.cmd_SAVE_VARIABLE(gc)

    def _now(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _z_is_valid(self):
        sv = self._vars()
        if self.z_var not in sv:
            return False
        # Sem estado gravado = offset anterior a este sistema: aceita-se.
        return sv.get(self.z_state_var, 'valid') == 'valid'

    def _xy_state(self):
        sv = self._vars()
        if self.x_var not in sv or self.y_var not in sv:
            return 'none'
        return sv.get(self.xy_state_var, 'final')

    def _store_z(self, offset):
        # Unico sitio onde o offset Z e' gravado (switch e papel).
        self.last_z = offset
        # Valor anterior, para comparacao na mensagem
        sv = self._vars()
        old_z = sv.get(self.z_var)
        old_when = sv.get(self.z_time_var, 'data desconhecida')
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%.4f" % (self.z_var, offset))
        self._save_var(self.z_state_var, 'valid')
        self._save_var(self.z_time_var, self._now())
        msg = "Offset Z do T1 gravado: Z=%.4f" % offset
        if old_z is not None:
            msg += ("\nAnterior (%s): Z=%.4f\nDiferenca: Z %+.3f mm"
                    % (old_when, float(old_z), offset - float(old_z)))
        self.gcode.respond_info(msg)
        # Um XY final medido antes deste Z deixa de ser final.
        if self._xy_state() == 'final':
            self._save_var(self.xy_state_var, 'provisional')
            self.gcode.respond_info(
                "O offset X/Y do T1 foi medido antes deste offset Z e passou a"
                " provisorio. Repete a calibracao X/Y para o fixar.")

    def cmd_IDEX_SAVE_XY_OFFSET(self, gcmd):
        x = gcmd.get_float('X')
        y = gcmd.get_float('Y')
        state = gcmd.get('STATE', 'FINAL').lower()
        if state not in ('final', 'provisional'):
            raise gcmd.error("STATE tem de ser FINAL ou PROVISIONAL.")
        # Ultima barreira: um X/Y final so' com offset Z valido.
        if state == 'final' and not self._z_is_valid():
            raise gcmd.error(
                "Offset X/Y NAO gravado: nao ha offset Z valido entre"
                " ferramentas. Calibra primeiro o offset Z (CALIBRATE_IDEX"
                " ou CALIBRATE_IDEX_Z MANUAL=1).")
        # Valores anteriores, para comparacao na mensagem
        sv = self._vars()
        old_x, old_y = sv.get(self.x_var), sv.get(self.y_var)
        old_when = sv.get(self.xy_time_var, 'data desconhecida')
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%.4f" % (self.x_var, x))
        self.gcode.run_script_from_command(
            "SAVE_VARIABLE VARIABLE=%s VALUE=%.4f" % (self.y_var, y))
        self._save_var(self.xy_state_var, state)
        self._save_var(self.xy_time_var, self._now())
        msg = ("Offset X/Y do T1 gravado (%s): X=%.4f Y=%.4f"
               % ("final" if state == 'final' else "provisorio", x, y))
        if old_x is not None and old_y is not None:
            msg += ("\nAnterior (%s): X=%.4f Y=%.4f\nDiferenca: X %+.3f mm, Y %+.3f mm"
                    % (old_when, old_x, old_y, x - old_x, y - old_y))
        gcmd.respond_info(msg)

    def cmd_IDEX_CHECK_XY_ALLOWED(self, gcmd):
        # Barreira de entrada das calibracoes X/Y isoladas (kTAMV e manual).
        # PROVISIONAL=1 so' e' usado por dentro dos fluxos completos.
        if gcmd.get_int('PROVISIONAL', 0):
            return
        if not self._z_is_valid():
            raise gcmd.error(
                "Calibracao X/Y bloqueada: nao ha offset Z valido entre"
                " ferramentas. Usa CALIBRATE_IDEX (ou"
                " CALIBRATE_IDEX XY_MANUAL=1), ou calibra primeiro o Z com"
                " CALIBRATE_IDEX_Z MANUAL=1.")
        when = self._vars().get(self.z_time_var, 'data desconhecida')
        gcmd.respond_info(
            "Offset Z do T1 calibrado em %s. Se o printcore de alguma"
            " ferramenta foi trocado desde entao, interrompe e calibra"
            " primeiro o offset Z." % when)

    def cmd_IDEX_CALIBRATION_STATUS(self, gcmd):
        sv = self._vars()
        labels = {'final': 'final', 'provisional': 'provisorio',
                  'invalid': 'invalido', 'none': 'nao calibrado'}

        def num(name):
            return "%.4f" % sv[name] if name in sv else "-"
        trim = sv.get('tool_1_z_trim')
        gcmd.respond_info(
            "Calibracao T1 (relativa ao T0)\n"
            "  Z:   %s  [%s, %s]%s\n"
            "  X/Y: %s / %s  [%s, %s]"
            % (num(self.z_var),
               "valido" if self._z_is_valid() else
               ("invalido" if self.z_var in sv else "nao calibrado"),
               sv.get(self.z_time_var, 'data desconhecida'),
               ("\n  Ajuste fino Z do T1: %+.3f (IDEX_Z_TRIM_T1)" % float(trim)
                if trim not in (None, 0, 0.0) else ""),
               num(self.x_var), num(self.y_var),
               labels.get(self._xy_state(), self._xy_state()),
               sv.get(self.xy_time_var, 'data desconhecida')))

    def cmd_IDEX_CHECK_DUAL_READY(self, gcmd):
        # Usado pelo PRINT_START quando a impressao usa o T1.
        if not self._z_is_valid():
            raise gcmd.error(
                "Impressao cancelada: usa as duas ferramentas, mas a altura"
                " entre bicos (Z) nao esta calibrada ou foi invalidada. Corre"
                " CALIBRATE_IDEX e volta a iniciar a impressao.")
        if self._xy_state() != 'final':
            raise gcmd.error(
                "Impressao cancelada: usa as duas ferramentas, mas a posicao"
                " entre bicos (X/Y) nao esta calibrada, esta provisoria ou foi"
                " invalidada. Corre CALIBRATE_IDEX e volta a iniciar"
                " a impressao.")

    def cmd_PRINTCORE_CHANGED(self, gcmd):
        # Os offsets do T1 sao relativos ao T0: trocar o printcore de
        # QUALQUER ferramenta invalida o par. Pensado para o futuro sensor.
        tool = gcmd.get_int('TOOL', minval=0, maxval=1)
        self._save_var(self.z_state_var, 'invalid')
        if self._xy_state() != 'none':
            self._save_var(self.xy_state_var, 'invalid')
        gcmd.respond_info(
            "Printcore do T%d trocado: calibracao entre ferramentas"
            " invalidada. Corre CALIBRATE_IDEX antes de imprimir com as"
            " duas ferramentas." % tool)
        if self._vars().get('tool_1_z_trim') not in (None, 0, 0.0):
            gcmd.respond_info(
                "Nota: o ajuste fino Z do T1 (IDEX_Z_TRIM_T1) continua a %+.3f mm."
                " Confirma-o na primeira impressao com o printcore novo."
                % float(self._vars()['tool_1_z_trim']))

    # ------------------------------------------------------------------ #
    # Sondagem no switch                                                 #
    # ------------------------------------------------------------------ #
    def _probe_once(self):
        toolhead = self.printer.lookup_object('toolhead')
        phoming = self.printer.lookup_object('homing')
        pos = toolhead.get_position()
        target = list(pos)
        target[2] = self.probe_z_min
        # probing_move devolve a posicao cinematica no instante do disparo,
        # sem a impor (ao contrario do G28).
        curpos = phoming.probing_move(self.z_endstop, target, self.probe_speed)
        # Recolhe um pouco para libertar o switch antes da proxima leitura.
        toolhead.manual_move([None, None, curpos[2] + self.sample_retract],
                             self.lift_speed)
        return curpos[2]

    def _probe_samples(self, gcmd):
        # Devolve (mediana, dispersao, lista_de_amostras).
        results = []
        for i in range(self.samples):
            z = self._probe_once()
            results.append(z)
            gcmd.respond_info("  amostra %d: z=%.4f" % (i + 1, z))
        ordered = sorted(results)
        median = ordered[len(ordered) // 2]
        dispersion = ordered[-1] - ordered[0]
        return median, dispersion, results

    def _goto_switch(self):
        # Sobe para altura segura e move para o ponto do switch. Usa G-code
        # normal (respeita o offset X/Y aplicado pelo T0/T1), garantindo que
        # o bico ativo aterra fisicamente no mesmo ponto do switch.
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G1 Z%.3f F%d" % (self.safe_z, int(self.lift_speed * 60)))
        self.gcode.run_script_from_command(
            "G1 X%.3f Y%.3f F6000" % (self.switch_x, self.switch_y))
        self.gcode.run_script_from_command("M400")

    def cmd_MEASURE_ACTIVE_NOZZLE_Z(self, gcmd):
        if not self._check_homed():
            raise gcmd.error("Faz G28 primeiro.")
        self._goto_switch()
        median, dispersion, _ = self._probe_samples(gcmd)
        self.last_z = median
        gcmd.respond_info(
            "Altura de disparo do bico ativo: z=%.4f (dispersao=%.4f)"
            % (median, dispersion))

    # ------------------------------------------------------------------ #
    # Fase de decisao (sanidade)                                         #
    # ------------------------------------------------------------------ #
    def _decide_and_save(self, gcmd, offset, disp0, disp1):
        worst = max(disp0, disp1)
        fails = worst > self.sanity_dispersion_max

        # Relatorio comum a todos os niveis (o "rasto" legivel).
        gcmd.respond_info(
            "--- Sanidade (nivel %d) ---\n"
            "Dispersao T0=%.4f  T1=%.4f  (pior=%.4f, limite=%.4f)"
            % (self.sanity_level, disp0, disp1, worst,
               self.sanity_dispersion_max))

        if self.sanity_level == 1:
            # Debug: grava sempre, mas simula o que os outros niveis fariam.
            if fails:
                gcmd.respond_info(
                    "AVISO: a dispersao excede o limite. No nivel 2 isto teria"
                    " sido REJEITADO; no nivel 3 teria BLOQUEADO. Provavel bico"
                    " sujo/folga. (Nivel 1: gravado na mesma para diagnostico.)")
            else:
                gcmd.respond_info("Sanidade OK.")
            self._save_offset(gcmd, offset)
            return

        if self.sanity_level == 2:
            if fails:
                msg = ("REJEITADO (nivel 2): dispersao acima do limite. O valor"
                       " antigo foi MANTIDO. Limpa o bico e repete, ou usa o"
                       " papel (CALIBRATE_IDEX_Z MANUAL=1).")
                # Dentro dos fluxos completos o Z TEM de ficar gravado para se
                # poder continuar: nesse caso a rejeicao para o fluxo.
                if gcmd.get_int('REQUIRE_SAVE', 0):
                    raise gcmd.error(msg)
                gcmd.respond_info(msg)
                return
            gcmd.respond_info("Sanidade OK.")
            self._save_offset(gcmd, offset)
            return

        # Nivel 3 (estrito)
        if fails:
            raise gcmd.error(
                "BLOQUEADO (nivel 3): medicao nao fiavel (dispersao %.4f >"
                " %.4f). Limpa o bico e repete, ou usa o teste do papel:"
                " CALIBRATE_IDEX_Z MANUAL=1." % (worst, self.sanity_dispersion_max))
        gcmd.respond_info("Sanidade OK.")
        self._save_offset(gcmd, offset)

    def _save_offset(self, gcmd, offset):
        self._store_z(offset)
        gcmd.respond_info(
            "Offset Z (T1 - T0) = %.4f mm -> gravado como %s."
            % (offset, self.z_var))

    # ------------------------------------------------------------------ #
    # Metodo 1: switch (automatico) + sanidade                           #
    # ------------------------------------------------------------------ #
    def cmd_CALIBRATE_IDEX_Z_OFFSET(self, gcmd):
        self._require_xy_offset(gcmd)
        if not self._check_homed():
            self.gcode.run_script_from_command("G28")

        # O estado G-code e' guardado DEPOIS de o T0 estar ativo e reposto
        # DEPOIS de voltar ao T0: assim os offsets restaurados sao sempre os
        # do T0. (Antes, se a medicao comecasse com o T1 ativo, o RESTORE
        # final punha os offsets X/Y/Z do T1 em cima do T0.)
        # CLEAN=1: cada ferramenta e' limpa imediatamente antes de tocar no
        # switch (macro _IDEX_CAL_CLEAN_TOOL, tool_clean.cfg), para nao haver
        # pasta na ponta a falsear a altura.
        clean = gcmd.get_int('CLEAN', 0)
        self.gcode.run_script_from_command("T0")
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=_IDEXZ")
        try:
            if clean:
                self.gcode.run_script_from_command("_IDEX_CAL_CLEAN_TOOL TOOL=0")
            gcmd.respond_info("A medir com T0...")
            self._goto_switch()
            z0, disp0, _ = self._probe_samples(gcmd)
            gcmd.respond_info("T0 -> z0=%.4f (dispersao=%.4f)" % (z0, disp0))

            gcmd.respond_info("A medir com T1...")
            if clean:
                self.gcode.run_script_from_command("_IDEX_CAL_CLEAN_TOOL TOOL=1")
            self.gcode.run_script_from_command("T1")
            self._goto_switch()
            z1, disp1, _ = self._probe_samples(gcmd)
            gcmd.respond_info("T1 -> z1=%.4f (dispersao=%.4f)" % (z1, disp1))

            offset = z1 - z0
            # A fase de decisao trata de gravar (ou nao) conforme o nivel.
            self._decide_and_save(gcmd, offset, disp0, disp1)
        finally:
            # Volta sempre ao T0 (tambem em caso de erro) antes de repor.
            self.gcode.run_script_from_command("T0")
            self.gcode.run_script_from_command(
                "RESTORE_GCODE_STATE NAME=_IDEXZ MOVE=0")

    # ------------------------------------------------------------------ #
    # Metodo 2: papel (manual, recurso) - bloco unico T0 -> T1            #
    # Um so comando: papel do T0 e, ao carregar ACCEPT, encadeia          #
    # automaticamente o papel do T1. So grava no fim.                     #
    # ------------------------------------------------------------------ #
    def _goto_bed_center_safe(self):
        self.gcode.run_script_from_command("G90")
        self.gcode.run_script_from_command(
            "G1 Z%.3f F%d" % (self.safe_z, int(self.lift_speed * 60)))
        self.gcode.run_script_from_command(
            "G1 X%.3f Y%.3f F6000" % (self.paper_bed_x, self.paper_bed_y))
        self.gcode.run_script_from_command("M400")

    def cmd_CALIBRATE_IDEX_Z_PAPER(self, gcmd):
        # Passo 1: papel do T0. Ao ACCEPT, o passo 2 (T1) arranca sozinho.
        # Nao exige offset X/Y: no prato, milimetros de diferenca em X/Y nao
        # afetam a medicao. Assim o papel serve de arranque numa maquina nova.
        # THEN=<macro>: macro a correr depois de o offset ficar gravado (usado
        # pela calibracao completa com o Z pelo papel, para seguir para o X/Y)
        then = gcmd.get('THEN', '').strip().upper()
        if then and not re.match(r'^[A-Z_][A-Z0-9_]*$', then):
            raise gcmd.error("THEN invalido: %s" % then)
        self._paper_then = then or None
        # CLEAN=1: cada ferramenta e' limpa imediatamente antes do papel
        self._paper_clean = bool(gcmd.get_int('CLEAN', 0))

        if not self._check_homed():
            self.gcode.run_script_from_command("G28")

        # O offset Z NAO e' alterado: a medicao usa a posicao cinematica
        # (kin_pos), que nao depende dos offsets. (Antes punha-se Z=0 aqui e
        # no T1, o que dessincronizava o _TOOLZ e deixava o T0 no fim com
        # offset Z = -tool_1_z_offset.)
        self.gcode.run_script_from_command("T0")
        if self._paper_clean:
            self.gcode.run_script_from_command("_IDEX_CAL_CLEAN_TOOL TOOL=0")
        self._goto_bed_center_safe()

        gcmd.respond_info(
            "PAPEL 1/2 (T0): baixa o bico ate o papel arrastar e carrega"
            " ACCEPT. O T1 arranca automaticamente a seguir. (ABORT cancela.)")
        manual_probe.ManualProbeHelper(self.printer, gcmd,
                                       self._paper_t0_finalize)

    def _paper_t0_finalize(self, kin_pos):
        # Chamado ao ACCEPT/ABORT do T0. Encadeia o T1 automaticamente.
        # IMPORTANTE: correr dentro deste callback nunca pode deixar uma
        # excecao propagar, senao o Klipper faz shutdown. Por isso o try.
        if kin_pos is None:
            self.gcode.respond_info("Papel cancelado no T0. Nada foi gravado.")
            self._paper_then_cancelled()
            return
        self._paper_z_t0 = kin_pos[2]
        try:
            self.gcode.respond_info(
                "T0 registado (z=%.4f). A preparar o T1..." % self._paper_z_t0)
            if getattr(self, '_paper_clean', False):
                self.gcode.run_script_from_command("_IDEX_CAL_CLEAN_TOOL TOOL=1")
            self.gcode.run_script_from_command("T1")
            self._goto_bed_center_safe()
            self.gcode.respond_info(
                "PAPEL 2/2 (T1): baixa o bico ate o papel arrastar e carrega"
                " ACCEPT para gravar. (ABORT cancela.)")
            new_gcmd = self.gcode.create_gcode_command(
                "CALIBRATE_IDEX_Z_PAPER", "CALIBRATE_IDEX_Z_PAPER", {})
            manual_probe.ManualProbeHelper(self.printer, new_gcmd,
                                           self._paper_t1_finalize)
        except Exception as e:
            self.gcode.respond_info(
                "Erro ao preparar o T1: %s. Calibracao cancelada, nada"
                " gravado." % str(e))
            self._paper_then_cancelled()

    def _paper_t1_finalize(self, kin_pos):
        if kin_pos is None:
            self.gcode.respond_info("Papel cancelado no T1. Nada foi gravado.")
            try:
                self.gcode.run_script_from_command("T0")
            except Exception as e:
                self.gcode.respond_info("Erro ao voltar ao T0: %s" % str(e))
            self._paper_then_cancelled()
            return
        try:
            z_t1 = kin_pos[2]
            # A base do T0 cancela-se: offset = altura_T1 - altura_T0, ambas
            # medidas frescas no prato nesta sessao.
            offset = z_t1 - self._paper_z_t0
            self._store_z(offset)
            self.gcode.respond_info(
                "PAPEL concluido. Offset Z (T1 - T0) = %.4f mm -> gravado"
                " como %s." % (offset, self.z_var))
            self.gcode.run_script_from_command("T0")
        except Exception as e:
            self.gcode.respond_info(
                "Erro ao gravar: %s. Nada foi gravado." % str(e))
            self._paper_then_cancelled()
            return
        # Seguir para o passo seguinte (ex.: X/Y da calibracao completa).
        # Dentro deste callback nenhuma excecao pode propagar (shutdown).
        then, self._paper_then = self._paper_then, None
        if then:
            try:
                self.gcode.run_script_from_command(then)
            except Exception as e:
                self.gcode.respond_info(
                    "Erro no passo seguinte da calibracao: %s. O offset Z ficou"
                    " gravado; o X/Y tem de ser feito (CALIBRATE_IDEX_XY)." % str(e))
                try:
                    self.gcode.run_script_from_command("_CALIB_VISION_OFF")
                except Exception:
                    pass

    def _paper_then_cancelled(self):
        if getattr(self, '_paper_then', None):
            self._paper_then = None
            self.gcode.respond_info(
                "A calibracao completa foi interrompida: o X/Y nao foi feito.")


def load_config(config):
    return IdexZOffset(config)
