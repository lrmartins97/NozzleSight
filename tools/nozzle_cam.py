"""
nozzle_cam.py - descobre o dispositivo da camara do bico (calibracao).

Os scripts de afinacao mudam a exposicao e a luz da camara diretamente
(v4l2-ctl), por isso precisam do caminho dela. Esse caminho depende da porta
USB e muda de maquina para maquina; esta funcao le-o do crowsnest.conf da
maquina, na camara que serve a imagem da calibracao (porta 8081, /webcam2 no
Mainsail), a mesma que o NozzleSight usa.

Para usar outra camara ou outro ficheiro, sem mexer no codigo:
  NOZZLE_CAM_DEVICE=/dev/v4l/by-path/...   caminho direto
  NOZZLE_CAM_PORT=8081                     porta da camara no crowsnest.conf
  CROWSNEST_CONF=~/printer_data/config/crowsnest.conf
"""
import os
import re
import sys

CROWSNEST_CONF = os.path.expanduser(
    os.environ.get("CROWSNEST_CONF", "~/printer_data/config/crowsnest.conf"))
NOZZLE_CAM_PORT = int(os.environ.get("NOZZLE_CAM_PORT", "8081"))


def nozzle_cam_device():
    direto = os.environ.get("NOZZLE_CAM_DEVICE")
    if direto:
        return direto
    try:
        with open(CROWSNEST_CONF) as f:
            texto = f.read()
    except OSError:
        sys.exit("Nao encontrei %s. Indica a camara com NOZZLE_CAM_DEVICE=/dev/v4l/by-path/..."
                 % CROWSNEST_CONF)
    # Uma secao por camara ativa ("[cam N]" no inicio da linha; as comentadas
    # comecam por "#" e ficam de fora)
    for secao in re.split(r"(?m)^\[cam [^\]]*\]", texto)[1:]:
        porta = re.search(r"(?m)^port:\s*(\d+)", secao)
        if not porta or int(porta.group(1)) != NOZZLE_CAM_PORT:
            continue
        disp = re.search(r"(?m)^device:\s*(\S+)", secao)
        if not disp or "PREENCHER" in disp.group(1):
            sys.exit("A camara da porta %d nao tem caminho no crowsnest.conf. Corre:"
                     " bash ~/printer_data/config/instalar.sh --camaras" % NOZZLE_CAM_PORT)
        if not os.path.exists(disp.group(1)):
            sys.exit("A camara do bico (%s) nao esta ligada. Liga-a ou corre --camaras."
                     % disp.group(1))
        return disp.group(1)
    sys.exit("Nao ha nenhuma camara ativa na porta %d no %s (a camara do bico e' a de"
             " /webcam2)." % (NOZZLE_CAM_PORT, CROWSNEST_CONF))
