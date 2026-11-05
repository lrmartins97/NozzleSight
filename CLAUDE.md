# CLAUDE.md — NozzleSight

Notas para trabalhar neste repositório (`lrmartins97/NozzleSight`). Onde diz
**(incerto)**, confirmar antes de confiar.

## 1. O que é

O NozzleSight é um fork do **kTAMV**, feito por Andrei Ignat (TypQxQ) e
publicado sob GPL v3. O fork mantém a licença e o crédito ao autor no readme.
O objetivo é calibrar os offsets entre as duas ferramentas da impressora
**M-BIND**, uma IDEX da Blocks que imprime pasta/gel com seringas. A calibração
mede:
- **XY** com a câmara de calibração (Nozzle Cam), montada no prato a olhar para
  cima;
- **Z** com um switch no prato.

Onde corre:
- Raspberry Pi 5 com Raspberry Pi OS Lite 64 bit e utilizador `blocks`;
- Klipper, Moonraker, Mainsail, KlipperScreen e Crowsnest, instalados pelo
  KIAUH;
- placas: Octopus Pro H723 e 2× FYSETC H36 Combo V2 por USB.

Âmbito:
- Só para máquinas M-BIND iguais, com 2 ferramentas (T0 e T1).
- **Não é portável, por decisão do utilizador.** Depende das macros e variáveis
  do repositório `lrmartins97/m-bind-config`, que é a pasta
  `~/printer_data/config` de cada máquina.

## 2. Nomes internos que NÃO se mudam

Ficaram com os nomes do kTAMV, para não partir máquinas instaladas nem o
instalador:

| Nome | Onde |
|---|---|
| `~/kTAMV` | pasta do clone na máquina |
| `kTAMV_server.service` | serviço systemd do servidor, porta **8085** |
| `[ktamv]` | secção de configuração do Klipper |
| `KTAMV_*` | comandos G-code herdados |
| `[update_manager nozzlesight]` | entrada do Moonraker. Tem de estar em **minúsculas**, porque o Moonraker não aceitou o nome com maiúsculas |

## 3. Arquitetura

```
extension/   módulos do Klipper (instalados por symlink em klipper/klippy/extras)
server/      servidor Flask (venv próprio): deteção do bico e matriz da câmara
tools/       scripts de afinação e diagnóstico da câmara, corridos à mão por SSH
doc/         procedimento_camara_ktamv.pdf (instalar e afinar uma câmara nova)
ktamv-macros.cfg, ktamv_automation_example.cfg   macros originais do kTAMV
install.sh, nginx_proxy.sh                        instalação
```

### extension/ (Klipper)

- **`ktamv.py`, `ktamv_utl.py`**: código do kTAMV (adaptado). Comandos:
  - KTAMV_CALIB_CAMERA, KTAMV_FIND_NOZZLE_CENTER, KTAMV_SET_ORIGIN;
  - KTAMV_GET_OFFSET, KTAMV_SIMPLE_NOZZLE_POSITION;
  - KTAMV_START_PREVIEW / KTAMV_STOP_PREVIEW, KTAMV_SEND_SERVER_CFG.

  Os módulos importam-se entre si com imports relativos, por exemplo
  `from .ktamv_utl import ...`.
- **`idex_z_offset.py`**: offset Z entre ferramentas com o switch do prato.
  Comandos:
  - CALIBRATE_IDEX_Z_OFFSET (CLEAN, REQUIRE_SAVE);
  - CALIBRATE_IDEX_Z_PAPER (CLEAN, THEN);
  - MEASURE_ACTIVE_NOZZLE_Z;
  - IDEX_SAVE_XY_OFFSET X Y STATE=FINAL|PROVISIONAL;
  - IDEX_CHECK_XY_ALLOWED, IDEX_CALIBRATION_STATUS, IDEX_CHECK_DUAL_READY;
  - PRINTCORE_CHANGED TOOL=.

  `sanity_level` 1/2/3. Grava em `variables.cfg`, pelo `[save_variables]` do
  m-bind-config.
- **`idex_camera_focus.py`**: `IDEX_CAMERA_FOCUS_SCAN`. Procura em Z a altura
  de foco da câmara de calibração. Aceita `Z_MIN`/`Z_MAX` e também
  `ZMIN`/`ZMAX`.
- **`tool_cameras.py`**: TOOL_CAMERAS_OFF/ON/STATUS. Desliga as câmaras das
  cabeças (portas do Crowsnest) durante a calibração. Opções `ports` e
  `max_off_time` (600 s).
- **`single_error.py`**: embrulha `gcode._respond_error`. Uma mensagem de erro
  igual repetida dentro de `window` segundos (1.0 por defeito, máximo 10)
  aparece só uma vez, e as repetições vão para o log. Se o atributo não existir
  na versão do Klipper, fica inativo.
  - Porquê: o Klipper volta a reportar o erro em cada nível de macros
    aninhadas, e o utilizador via 5 ou mais notificações iguais.
  - A secção `[single_error]` está no m-bind-config
    (`impressao/klipper_base.cfg`).

### server/ (Flask, porta 8085)

- `ktamv_server.py`: rotas
  - `/getNozzlePosition`, `/getFocusScore`, `/hasMatrix`;
  - `/image`, `/preview`;
  - `/set_server_cfg`, `/calculate_camera_to_space_matrix`,
    `/calculate_offset_from_matrix`;
  - `/getReqest`, `/getAllReqests`, `/`.
- `ktamv_server_dm.py`, `ktamv_server_io.py`: estado e entrada/saída do
  servidor.
- **`nozzle_detector.py`**: detetor próprio, que substitui a deteção do kTAMV
  original. É por simetria radial, em 4 passos:
  1. votação de gradientes;
  2. evidência de anel (mediana do gradiente radial);
  3. centro por mínimos quadrados a partir dos gradientes;
  4. refinamento polar pela linha escura.

  Guarda memória/lock entre frames e passa `ref_radius` entre ferramentas.
  Se não tiver certeza, **devolve None em vez de um centro errado**. Decisão:
  um "não encontrei" é preferível a um offset errado gravado.
- `standby.jpg`: imagem mostrada quando não há frame.

### tools/ (diagnóstico, à mão)

Scripts:
- `ktamv_capture_frames.py`, `ktamv_capture_grid.py`;
- `ktamv_detector_test.py`, `ktamv_repeat_test.py`;
- `ktamv_find_exposure.py`, `ktamv_find_focus.py`, `ktamv_find_z.py`;
- `ktamv_light_test.py`.

**`nozzle_cam.py`** devolve o `/dev/v4l/by-path/...` da câmara de calibração:
- lê o `~/printer_data/config/crowsnest.conf` e escolhe o `[cam N]` com
  `port: 8081`;
- sai com mensagem clara se o ficheiro faltar, se o caminho for `PREENCHER` ou
  se a câmara não estiver ligada;
- overrides por variável de ambiente: `NOZZLE_CAM_DEVICE`, `NOZZLE_CAM_PORT`,
  `CROWSNEST_CONF`.

Porquê: o caminho depende da porta USB e muda de máquina para máquina, por isso
não pode estar no código.

Usam-no os 5 scripts que mexem no dispositivo: capture_frames, detector_test,
find_exposure, find_focus e light_test. Os outros 3 (capture_grid, find_z,
repeat_test) usam só o snapshot `http://localhost/webcam2/snapshot?max_delay=0`
e o Moonraker em `http://localhost:7125`.

### Macros e configuração (estão no m-bind-config, não aqui)

- `[ktamv]`:
  - `nozzle_cam_url: http://localhost/webcam2/snapshot?max_delay=0`;
  - `server_url: http://localhost:8085`;
  - `move_speed: 1800`, `send_frame_to_cloud: false`,
    `detection_tolerance: 1`.
- `[tool_cameras] ports: 8082, 8083`.
- `[idex_camera_focus] z_min: 2.0`.
- `[idex_z_offset]`:
  - `switch_position: 83, 113`;
  - `probe_speed 2`, `lift_speed 6`, `samples 5`, `sample_retract_dist 2`;
  - `probe_z_min -2`, `safe_z 10`;
  - `sanity_level 1`, `sanity_dispersion_max 0.05`;
  - `paper_bed_x 50`, `paper_bed_y 70`.
- `_idex_vars`:
  - `camera_x 22`, `camera_y 113`, `camera_z 6.7`;
  - `camera_led`, `calib_tool_cams_off`;
  - `park_t0_x -75`, `park_t1_x 175`, `hop_mm 6`.
- `calibracao/xy_camara.cfg`: `_IDEX_CAL_XY_AUTO`, `_IDEX_XY_AUTO`, mira manual
  (`_GOTO`, `_CAMERA`, `_ORIGIN_T0`, `_SET_ORIGIN`, `_ALIGN_T1`, `_SAVE`) e
  `_IDEX_SAVE_CAMERA_CAL`.
- `CALIBRATE_IDEX` (`comandos.cfg`):
  - parâmetros Z_MANUAL, XY_MANUAL, RECALIBRAR_CAMARA, LIMPEZA;
  - ordem: XY provisório → Z → XY final.
- T0/T1 aplicam os offsets guardados em `variables.cfg`.
- Ficheiros que só existem em cada máquina: `variables.cfg`,
  `ktamv_camera_matrix.json`, `crowsnest.conf`.

Uma alteração que mude nomes de comandos ou parâmetros obriga a mudar também o
m-bind-config.

## 4. Instalar, atualizar, testar

### Máquina nova

O `instalar.sh` do m-bind-config já instala o NozzleSight. À mão:

```
git clone https://github.com/lrmartins97/NozzleSight.git ~/kTAMV
bash ~/kTAMV/install.sh
```

O `install.sh`:
- instala os pacotes. Usa `libopenblas-dev`, porque o `libatlas` já não existe
  no Debian atual;
- cria o venv e o serviço `kTAMV_server`. Se o serviço já existir, entra em
  modo atualização;
- faz os symlinks dos 6 módulos de `extension/` para `klippy/extras`;
- escreve `[update_manager nozzlesight]` no `moonraker.conf`, com a origem
  tirada do clone:
  - deteta uma entrada antiga `ktamv`/`nozzlesight` sem ligar a maiúsculas;
  - nunca duplica;
- no Klipper, procura `[ktamv]` e `CALIB_CAMERA_KTAMV` em todos os ficheiros da
  config. Só insere o que faltar, antes do bloco SAVE_CONFIG;
- os backups usam `nextfilename`, corrigido para pastas com pontos no nome;
- no fim corre `nginx_proxy.sh`. Se este falhar, a instalação não falha.

`nginx_proxy.sh`:
- acrescenta `location /ktamv/ → http://127.0.0.1:8085/` (`proxy_buffering
  off`) ao site do Mainsail;
- guarda um backup em `sites-available/mainsail.antes-nozzlesight`;
- corre `nginx -t`. Se falhar, repõe o backup; se passar, faz reload.

Porquê: a câmara no Mainsail usa o URL relativo `/ktamv/image` em vez de
`http://<ip>:8085/...`, e assim funciona em qualquer rede. No Mainsail a câmara
fica com URL Snapshot `/ktamv/image`, tipo Adaptive MJPEG-Streamer, 4 fps.

### Atualizar uma máquina

```
cd ~/kTAMV && git pull
bash ~/kTAMV/install.sh
```

- O `git pull` sozinho **não chega quando aparece um módulo novo** em
  `extension/`. O symlink para `klippy/extras` só é criado pelo `install.sh`,
  que, com o serviço já instalado, corre em modo atualização: refaz os links,
  corre o `nginx_proxy.sh`, reinicia o `kTAMV_server` e o Klipper.
- Se só mudou código de módulos que já existem: `git pull` e
  `sudo systemctl restart klipper`. Se mudou `server/`: também
  `sudo systemctl restart kTAMV_server`.
- O update manager do Moonraker faz o `git pull`, mas não cria os symlinks
  novos.
- **Ordem:** quando uma alteração mexe nos dois repositórios, atualiza-se o
  NozzleSight antes do m-bind-config. Por exemplo, o `[single_error]` está no
  m-bind-config. Numa máquina sem o `single_error.py` ligado, o Klipper recusa
  arrancar com a secção desconhecida.

### Testar

- Não há testes automáticos no repositório.
- O que se fez até agora:
  - verificar a sintaxe (`python3 -m py_compile`, `bash -n`);
  - testar a lógica com um Klipper simulado (objetos falsos), feito numa
    sessão anterior do Claude. **Esse harness não está no repositório**: se
    for preciso, faz-se outro;
  - testar na máquina real, com `klippy.log` e o Mainsail.
- Na máquina, para ler o log usar `grep -a`, porque o `klippy.log` é tratado
  como binário.
- O detetor testa-se com `tools/ktamv_detector_test.py` e
  `ktamv_repeat_test.py`, com a câmara ligada.

## 5. Câmara de calibração (factos medidos)

- Câmara: 3DO Nozzle Camera **V2.5** (Sony IMX258). A máquina 1 usava uma V2
  antes.
- **Foco fixo.** A lente roda à mão. Os controlos de foco do v4l2 são falsos e
  não fazem nada. Depois de focar, fixar a lente e correr de novo
  `CALIBRATE_IDEX_CAMERA_FOCUS`.
- **Os LEDs não se controlam por software.**
- Os nomes dos controlos v4l2 são os mesmos da V2.
- No Crowsnest é a câmara da **porta 8081** (`/webcam2`), com `v4l2ctl` em
  exposição manual 600 e WB 4600.
  - A exposição 600 foi afinada para a V2. **É preciso reconfirmar para a
    V2.5** com `ktamv_find_exposure.py`.
- Valores da máquina 1 (2026-10-08), só como referência:
  - `camera_mpp` 0.021, `camera_focus_z` 6.781;
  - T1: x −0.755, y −1.932, z 1.38.

## 6. Convenções

- Mensagens ao utilizador, readme e comentários em **português europeu, sem
  acentos no código** (como nos ficheiros atuais). Unidades em mm.
- Commits:
  - mensagem curta em português, no formato `ficheiro/área: o que muda`, por
    exemplo `single_error.py: cada erro aparece uma so' vez`;
  - cada commit acaba com as linhas de co-autor.
- Klipper:
  - nada de algarismos no meio dos nomes das macros;
  - o Jinja renderiza o template inteiro antes de executar, por isso valores
    lidos depois de um comando na mesma macro estão desatualizados;
  - os erros de macros aninhadas repetem-se em cada nível.
- Forma de trabalhar pedida pelo utilizador:
  - **propor antes de alterar** e esperar OK antes de commit/push;
  - explicar ficheiro a ficheiro o que muda;
  - acabar com os comandos exatos a correr na máquina, passo a passo;
  - não pôr caminhos de câmaras, números de série nem nada específico de uma
    máquina no GitHub.
- **Nunca** pôr tokens ou passwords no repositório nem nas notas.
- Para fazer push, a conta `lrmartins97` precisa da Claude GitHub App instalada
  com acesso ao repositório. Sem isso dá 403.

## 7. Estado

### Feito

- Correção do instalador (libopenblas), update_manager `nozzlesight`.
- Readme novo em português: todos os comandos, secção "Limites", agradecimento
  ao autor do kTAMV, sem a documentação antiga (os comandos antigos induzem em
  erro).
- `nginx_proxy.sh` e URL `/ktamv/image`.
- `single_error.py`.
- `Z_MIN`/`Z_MAX` no foco.
- `nozzle_cam.py` nos scripts.
- Máquina 2 instalada com o m-bind-config.

### Falta

- **Máquina 1** já corre o fork, mas **ainda não tem os últimos commits**:
  `single_error.py`, `nginx_proxy.sh`, `nozzle_cam.py` e `Z_MIN`/`Z_MAX`.
  Por fazer, por esta ordem:
  1. `cd ~/kTAMV && git pull && bash ~/kTAMV/install.sh` (cria o symlink do
     `single_error.py` e a rota `/ktamv/` no nginx);
  2. `cd ~/printer_data/config && git pull` (m-bind-config);
  3. `bash ~/printer_data/config/instalar.sh --camaras`, para criar o
     `crowsnest.conf` da máquina;
  4. no Mainsail, mudar a câmara do NozzleSight para `/ktamv/image`.

  **Por confirmar:** se o m-bind-config já foi atualizado na máquina 1. Se foi
  sem o passo 1, o Klipper deve estar a dar erro na secção `[single_error]`.
- **Máquina 2:** calibrações (foco, exposição V2.5, CALIBRATE_IDEX) depois de a
  mecânica estar montada.
- **Decisão pendente** (está no m-bind-config): pôr os defaults de
  CALIBRATE_IDEX em `XY_MANUAL=1 LIMPEZA=0`, com o PRINT_START a chamar
  `CALIBRATE_IDEX XY_MANUAL=0 LIMPEZA=1`.
- `TODO.txt` herdado do kTAMV (pausa depois do movimento, log para ficheiro,
  timers). Não foi revisto.

### Problemas conhecidos

1. **A deteção XY automática não é fiável**: aparece "No nozzle found". Por
   agora recomenda-se a mira manual (`XY_MANUAL=1`).
2. **Retry morto em `ktamv.py` `_calibrate_nozzle`** (cerca da linha 394–440):
   - a tentativa de mexer ±0.1 mm até 4 vezes quando não encontra o bico nunca
     corre, porque `utl.get_nozzle_position` lança `NozzleNotFoundException` em
     vez de devolver None;
   - o bug vem do kTAMV original;
   - a correção foi proposta e **não foi feita** (sem aprovação).
3. As macros originais do kTAMV (`ktamv-macros.cfg`, `ktamv_originais.cfg` na
   config) não são usadas pelo fluxo da M-BIND.
4. Pi 5 com o mesmo caminho by-path para portas diferentes: o udev cria links
   `usb-` e `usbv2-` por câmara. A identificação no `instalar.sh` deduplica pelo
   alvo do readlink e prefere `usb-`. Isto é do m-bind-config, mas afeta o
   `nozzle_cam.py`.
