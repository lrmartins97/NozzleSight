# single_error.py - mostra cada erro uma so' vez
#
# Quando um comando da erro dentro de macros, o Klipper volta a mostrar o mesmo
# erro em cada macro da cadeia, ao passar o erro para a macro de cima
# (gcode.py: cada nivel escreve o erro e volta a lancar). Uma calibracao com
# cinco niveis de macros mostra o mesmo "!!" cinco vezes, e o Mainsail mostra
# cinco notificacoes.
#
# Este modulo deixa passar so' a primeira: um erro com o mesmo texto do
# anterior, dentro de "window" segundos, nao e' mostrado outra vez. A paragem
# da macro e o evento de erro continuam iguais; so' muda o que aparece no ecra.
# Cada repeticao fica registada no klippy.log numa linha curta.
#
# Configuracao:
#   [single_error]
#   #window: 1.0    # segundos; os repetidos da mesma cadeia chegam em
#                   # milissegundos
#
# Coloca este ficheiro em: ~/klipper/klippy/extras/single_error.py

import logging


class SingleError:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.window = config.getfloat('window', 1.0, above=0., maxval=10.)
        self.last_msg = None
        self.last_time = -1e9
        gcode = self.printer.lookup_object('gcode')
        self.orig = getattr(gcode, '_respond_error', None)
        if self.orig is None:
            # Klipper mudou por dentro: fica tudo como no Klipper normal
            logging.warning("single_error: gcode._respond_error nao existe"
                            " nesta versao do Klipper; modulo inativo.")
            return
        gcode._respond_error = self._respond_error

    def _respond_error(self, msg):
        now = self.reactor.monotonic()
        if msg == self.last_msg and now - self.last_time < self.window:
            self.last_time = now
            logging.info("single_error: o mesmo erro, repetido pela macro de"
                         " cima, nao foi mostrado outra vez")
            return
        self.last_msg = msg
        self.last_time = now
        self.orig(msg)


def load_config(config):
    return SingleError(config)
