# tool_cameras.py - desliga/religa as camaras das ferramentas (e os seus LEDs)
#
# As camaras das ferramentas (tool 0 / tool 1) tem LEDs que so apagam quando a
# camara deixa de transmitir. Durante a medicao X/Y com a camara do bico esses
# LEDs criam reflexos nas pontas de inox, por isso sao desligados:
#   - DESLIGAR: termina so os processos ustreamer dessas camaras (as portas
#     indicadas). A camara do bico e as outras continuam a transmitir.
#   - RELIGAR: reinicia o servico do crowsnest atraves do Moonraker, que volta
#     a arrancar todas as camaras (a camara do bico pisca alguns segundos).
#
# Seguranca: se as camaras ficarem desligadas mais de max_off_time (por
# exemplo, uma calibracao que parou com erro), sao religadas sozinhas. Se o
# Klipper reiniciar com as camaras desligadas por este modulo, tambem.
#
# Comandos:
#   TOOL_CAMERAS_OFF / TOOL_CAMERAS_ON / TOOL_CAMERAS_STATUS
#
# Configuracao (printer.cfg ou ficheiro incluido):
#   [tool_cameras]
#   ports: 8082, 8083          # portas das camaras das ferramentas no crowsnest
#   #service: crowsnest
#   #moonraker_url: http://127.0.0.1:7125
#   #max_off_time: 600         # segundos
#
# Coloca este ficheiro em: ~/klipper/klippy/extras/tool_cameras.py
import json, logging, os, subprocess, threading, urllib.request, urllib.parse

FLAG_FILE = "/tmp/klipper_tool_cameras_off"


class ToolCameras:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.ports = [int(p) for p in config.get('ports').split(',') if p.strip()]
        self.service = config.get('service', 'crowsnest')
        self.moonraker = config.get('moonraker_url', 'http://127.0.0.1:7125')
        self.max_off = config.getfloat('max_off_time', 600., minval=60.)
        self.off_by_us = False
        self.watchdog = self.reactor.register_timer(self._watchdog)
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.gcode.register_command(
            'TOOL_CAMERAS_OFF', self.cmd_TOOL_CAMERAS_OFF,
            desc="Desliga as camaras das ferramentas (apaga os LEDs)")
        self.gcode.register_command(
            'TOOL_CAMERAS_ON', self.cmd_TOOL_CAMERAS_ON,
            desc="Religa as camaras das ferramentas (reinicia o crowsnest)")
        self.gcode.register_command(
            'TOOL_CAMERAS_STATUS', self.cmd_TOOL_CAMERAS_STATUS,
            desc="Mostra que camaras das ferramentas estao a transmitir")

    # --- processos -------------------------------------------------------
    def _pattern(self, port):
        # O espaco no fim evita que 8082 apanhe tambem 80820, etc.
        return "ustreamer --host 127.0.0.1 --port %d " % port

    def _running_ports(self):
        running = []
        for p in self.ports:
            r = subprocess.run(["pgrep", "-f", self._pattern(p)],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                running.append(p)
        return running

    # --- religar via Moonraker (numa thread: nao bloqueia o Klipper) -----
    def _restart_service(self):
        def worker():
            msg = None
            try:
                url = "%s/machine/services/restart?%s" % (
                    self.moonraker, urllib.parse.urlencode({'service': self.service}))
                req = urllib.request.Request(url, method="POST")
                with urllib.request.urlopen(req, timeout=15) as r:
                    json.load(r)
            except Exception as e:
                msg = ("Nao foi possivel religar as camaras das ferramentas"
                       " (%s). Reinicia o crowsnest no Mainsail." % (str(e),))
                logging.exception("tool_cameras: erro ao reiniciar o %s", self.service)
            if msg:
                self.reactor.register_async_callback(
                    lambda et, m=msg: self.gcode.respond_info(m))
        threading.Thread(target=worker, daemon=True).start()

    def _set_flag(self, on):
        try:
            if on:
                open(FLAG_FILE, "w").close()
            elif os.path.exists(FLAG_FILE):
                os.remove(FLAG_FILE)
        except OSError:
            logging.exception("tool_cameras: erro no ficheiro de estado")

    def _turn_on(self):
        self.off_by_us = False
        self._set_flag(False)
        self.reactor.update_timer(self.watchdog, self.reactor.NEVER)
        self._restart_service()

    # --- eventos ---------------------------------------------------------
    def _handle_ready(self):
        # Klipper reiniciou com as camaras desligadas por este modulo: religa
        if os.path.exists(FLAG_FILE):
            self.gcode.respond_info(
                "As camaras das ferramentas tinham ficado desligadas: a religar.")
            self._turn_on()

    def _watchdog(self, eventtime):
        if self.off_by_us:
            self.gcode.respond_info(
                "As camaras das ferramentas estavam desligadas ha mais de %d s:"
                " religadas automaticamente." % (int(self.max_off),))
            self._turn_on()
        return self.reactor.NEVER

    # --- comandos --------------------------------------------------------
    def cmd_TOOL_CAMERAS_OFF(self, gcmd):
        if not self._running_ports():
            if not self.off_by_us:
                gcmd.respond_info("As camaras das ferramentas ja estao desligadas.")
            return
        for p in self.ports:
            subprocess.run(["pkill", "-f", self._pattern(p)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Espera ate 3 s que os processos terminem
        eventtime = self.reactor.monotonic()
        for _ in range(30):
            if not self._running_ports():
                break
            eventtime = self.reactor.pause(eventtime + 0.1)
        still = self._running_ports()
        self.off_by_us = True
        self._set_flag(True)
        self.reactor.update_timer(self.watchdog,
                                  self.reactor.monotonic() + self.max_off)
        if still:
            raise gcmd.error(
                "Nao foi possivel desligar as camaras das ferramentas (portas %s)."
                " Os LEDs podem causar reflexos: calibracao interrompida."
                % (", ".join(str(p) for p in still),))
        gcmd.respond_info("Camaras das ferramentas desligadas (LEDs apagados).")

    def cmd_TOOL_CAMERAS_ON(self, gcmd):
        if not self.off_by_us and len(self._running_ports()) == len(self.ports):
            return
        gcmd.respond_info("A religar as camaras das ferramentas...")
        self._turn_on()

    def cmd_TOOL_CAMERAS_STATUS(self, gcmd):
        running = self._running_ports()
        gcmd.respond_info(
            "Camaras das ferramentas: %s" % ", ".join(
                "%d %s" % (p, "a transmitir" if p in running else "desligada")
                for p in self.ports))

    def get_status(self, eventtime):
        return {'off': self.off_by_us}


def load_config(config):
    return ToolCameras(config)
