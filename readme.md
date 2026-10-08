# NozzleSight

Calibração entre ferramentas para impressoras Klipper com várias cabeças (IDEX).
Mede com uma câmara virada para cima a posição X/Y de cada bico e, com o endstop Z,
a diferença de altura entre bicos. Grava os offsets da segunda ferramenta em relação
à primeira e impede imprimir com as duas cabeças sem calibração válida.

Foi desenvolvido na impressora M-BIND (seringas, dois bicos), mas a base serve para
mais ferramentas.

## Agradecimento

O NozzleSight é um fork do **[kTAMV](https://github.com/TypQxQ/kTAMV)**, criado por
**Andrei Ignat ([TypQxQ](https://github.com/TypQxQ))**. A ligação ao Klipper, o servidor
de visão e o método de calibração da câmara são trabalho dele, e sem isso este projeto
não existia. Obrigado, Andrei, por o teres publicado em código aberto.

Licença: GPL v3, a mesma do kTAMV (ver `LICENSE`). A documentação original do kTAMV está
no fim deste ficheiro.

## Instalar

```
cd ~/ && git clone https://github.com/lrmartins97/NozzleSight.git kTAMV && bash ~/kTAMV/install.sh
```

A pasta chama-se `~/kTAMV` e os nomes internos são os do kTAMV (secção `[ktamv]`, serviço
`kTAMV_server`), para que as configurações existentes continuem a funcionar.

O instalador:
- instala os pacotes do sistema e o ambiente Python (`~/ktamv-env`);
- liga os módulos ao Klipper (`~/klipper/klippy/extras/`) com atalhos, por isso uma
  atualização do repositório chega logo ao Klipper;
- instala o serviço `kTAMV_server` (porta 8085);
- acrescenta `[update_manager NozzleSight]` ao `moonraker.conf`, se ainda não existir (também reconhece o nome antigo `ktamv`, para não duplicar);
- acrescenta `[ktamv]` e as macros à configuração do Klipper **só se ainda não existirem**
  em nenhum ficheiro da configuração (procura também nos ficheiros incluídos), e escreve-as
  antes do bloco do SAVE_CONFIG. Faz cópia dos ficheiros que altera.

Pode ser corrido outra vez numa máquina já instalada: funciona como atualização.
Funciona em Debian 11, 12 e 13 (Raspberry Pi OS).

Atualizar: no Mainsail, Máquina → Atualizações → `NozzleSight`.

## Como funciona a calibração

A ordem é **XY provisório → Z → XY final**:
1. **XY provisório**: os dois bicos têm de tocar no mesmo ponto do switch Z.
2. **Z**: diferença de altura entre os bicos, medida no switch.
3. **XY final**: com o Z certo, os dois bicos ficam à mesma distância da câmara (foco).

O estado de cada offset (válido, provisório, inválido) e a data da medição ficam no
`variables.cfg`. A calibração XY recusa começar sem Z válido, e uma impressão com as duas
cabeças é recusada sem Z e XY final válidos.

As macros de alto nível (por exemplo `CALIBRATE_IDEX`, que limpa os bicos, liga a luz e
corre tudo pela ordem certa) são da configuração de cada máquina e não estão neste
repositório. Os comandos abaixo são os blocos que elas usam.

## Comandos do Klipper

### Câmara do bico, X/Y (`ktamv.py`, secção `[ktamv]`)
| Comando | Para que serve |
|---|---|
| `KTAMV_CALIB_CAMERA` | Calibra a câmara: move o bico à volta do ponto atual para saber quantos mm vale cada pixel. |
| `KTAMV_FIND_NOZZLE_CENTER` | Deteta o bico na imagem e centra-o na câmara. |
| `KTAMV_SET_ORIGIN` | Guarda a posição atual como origem (normalmente com o T0 centrado). |
| `KTAMV_GET_OFFSET` | Mostra a distância X/Y entre a posição atual e a origem. |
| `KTAMV_SIMPLE_NOZZLE_POSITION` | Diz se vê um bico na imagem, sem mexer a máquina. |
| `KTAMV_SEND_SERVER_CFG` | Envia ao servidor o URL da câmara (`CAMERA_URL=` opcional). |
| `KTAMV_START_PREVIEW` / `KTAMV_STOP_PREVIEW` | Liga/desliga a pré-visualização com a deteção desenhada. |

### Foco da câmara (`idex_camera_focus.py`, secção `[idex_camera_focus]`)
| Comando | Para que serve |
|---|---|
| `IDEX_CAMERA_FOCUS_SCAN [Z_MIN= Z_MAX=] [SAVE=0\|1]` | Percorre o Z com o T0 em cima da câmara, encontra a altura em que a ponta fica mais nítida e grava-a (`camera_focus_z`). Sem Z_MIN/Z_MAX percorre `range` mm para cada lado do foco atual. Só grava se o resultado for fiável. |
| `IDEX_KTAMV_MATRIX_CHECK` | Pergunta ao servidor se já tem a calibração da câmara (fica em `printer.idex_camera_focus.matrix_ok`). |

Opções: `server_url`, `range` (1.0), `step` (0.05), `frames` (3), `settle_time` (0.5),
`z_min` (3.0, limite de segurança), `min_margin` (0.15), `z_speed` (5).

### Offset Z e estado da calibração (`idex_z_offset.py`, secção `[idex_z_offset]`)
| Comando | Para que serve |
|---|---|
| `CALIBRATE_IDEX_Z_OFFSET [CLEAN=0\|1] [REQUIRE_SAVE=0\|1]` | Automático: sonda os dois bicos no switch Z e grava `tool_1_z_offset = z1 − z0`. Verifica a dispersão entre amostras (ver `sanity_level`). `CLEAN=1` limpa cada bico antes de medir. |
| `CALIBRATE_IDEX_Z_PAPER [CLEAN=0\|1] [THEN=<macro>]` | Manual: teste do papel com o T0 e logo a seguir com o T1. Não precisa de SAVE_CONFIG nem de reiniciar. |
| `MEASURE_ACTIVE_NOZZLE_Z` | Mede só o bico ativo no switch (diagnóstico). |
| `IDEX_SAVE_XY_OFFSET X= Y= [STATE=FINAL\|PROVISIONAL]` | Única forma de gravar o offset X/Y do T1. `FINAL` exige Z válido. |
| `IDEX_CHECK_XY_ALLOWED [PROVISIONAL=1]` | Barreira: recusa começar uma calibração X/Y sem Z válido. |
| `IDEX_CALIBRATION_STATUS` | Mostra os offsets do T1, o estado e as datas. |
| `IDEX_CHECK_DUAL_READY` | Usado no PRINT_START: cancela uma impressão com as duas cabeças sem calibração válida. |
| `PRINTCORE_CHANGED TOOL=0\|1` | Marca a calibração como inválida depois de trocar um printcore. |

Opções principais: `switch_position` (X,Y do switch, obrigatório), `samples` (3),
`probe_speed` (5), `lift_speed` (10), `sample_retract_dist` (2), `probe_z_min` (−2),
`safe_z` (10), `paper_bed_x`/`paper_bed_y` (50/70), `sanity_dispersion_max` (0.05 mm) e
`sanity_level`:
- 1: reporta tudo mas grava sempre (bancada/testes);
- 2: se a dispersão passar do limite, não grava e mantém o valor antigo;
- 3: se passar, bloqueia e manda para o teste do papel.

### Câmaras das ferramentas (`tool_cameras.py`, secção `[tool_cameras]`)
| Comando | Para que serve |
|---|---|
| `TOOL_CAMERAS_OFF` | Desliga as câmaras das ferramentas (e os LEDs delas, que fazem reflexos nas pontas). A câmara do bico continua. |
| `TOOL_CAMERAS_ON` | Volta a ligá-las (reinicia o crowsnest). |
| `TOOL_CAMERAS_STATUS` | Mostra quais estão a transmitir. |

Opções: `ports` (portas das câmaras das ferramentas, obrigatório), `service` (crowsnest),
`moonraker_url`, `max_off_time` (600 s: religa sozinho se uma calibração parar a meio).

### Macros do kTAMV (`ktamv-macros.cfg`)
`CALIB_CAMERA_KTAMV`, `FIND_NOZZLE_CENTER_KTAMV`, `SET_ORIGIN_KTAMV`, `MOVE_TO_ORIGIN_KTAMV`,
`SIMPLE_NOZZLE_POSITION_KTAMV`, `GET_OFFSET_KTAMV`, `PRINT_OFFSET_KTAMV`,
`SEND_SERVER_CFG_KTAMV`, `START_PREVIEW_KTAMV`, `STOP_PREVIEW_KTAMV`: as originais do kTAMV,
para uso manual.

## Servidor de visão (`server/`)
Serviço `kTAMV_server`, porta 8085. Recebe as imagens da câmara e devolve a posição do bico.
- `nozzle_detector.py`: detetor novo. Procura os círculos concêntricos da ponta (furo,
  parede, borda) em vez da "zona mais nítida", que com pontas de inox é muitas vezes um
  reflexo. Centro com precisão sub-pixel; devolve "não vi" em vez de um centro errado.
- `ktamv_server.py` / `ktamv_server_dm.py`: servidor do kTAMV com o detetor novo e os
  pedidos usados pelo foco (`/getFocusScore`, `/hasMatrix`).

## Scripts de afinação (`tools/`)
Correm no Pi, com o ambiente do NozzleSight:
`~/ktamv-env/bin/python ~/kTAMV/tools/<script> ...`

| Script | Para que serve |
|---|---|
| `ktamv_find_focus.py` | Encontra o melhor `focus_absolute` da câmara (câmaras com foco ajustável). |
| `ktamv_find_z.py [zmin zmax]` | Encontra a melhor altura Z para a câmara de foco fixo e a margem de Z com imagem nítida. |
| `ktamv_find_exposure.py` | Escolhe a exposição pelo desempenho real do detetor. |
| `ktamv_light_test.py` | Testa intensidade do anel de LEDs × exposição. |
| `ktamv_capture_frames.py <exposições...>` | Guarda imagens cruas com várias exposições. |
| `ktamv_capture_grid.py NOME` | Move o bico numa grelha 3×3 conhecida e guarda imagens, para validar o detetor. |
| `ktamv_detector_test.py frames\|grid <pasta>` | Compara o detetor novo com o antigo nas imagens capturadas. |
| `ktamv_repeat_test.py` | Corre `CALIBRATE_IDEX_XY` várias vezes seguidas e regista resultado e offset de cada corrida. |

Os scripts têm no início o caminho da câmara (`DEVICE`), o URL da imagem (`SNAPSHOT_URL`) e
o do Moonraker. Numa máquina com outra câmara ou outra porta, muda esses valores antes de
correr.

## Câmara nova
Procedimento para instalar e afinar uma câmara: `doc/procedimento_camara_ktamv.pdf`.

---

# Documentação original do kTAMV

<p align="center">
  <h1 align="center">kTAMV - Klipper Tool Alignment (using) Machine Vision</h1>
  <img src="doc/mainsail_main.jpg?raw=true" alt='screenshot of UI' width='800'>
</p>

This allows X and Y allignment betwween multiple tools on a 3D printer using a camera that points up towards the nozzle from inside [Klipper](https://github.com/Klipper3d/klipper).
<p align="center">
  <a aria-label="Downloads" href="https://github.com/TypQxQ/kTAMV/releases">
    <img src="https://img.shields.io/github/release/TypQxQ/kTAMV?display_name=tag&style=flat-square">
  </a>
  <a aria-label="Stars" href="https://github.com/TypQxQ/kTAMV/stargazers">
    <img src="https://img.shields.io/github/stars/TypQxQ/kTAMV?style=flat-square">
  </a>
  <a aria-label="Forks" href="https://github.com/TypQxQ/kTAMV/network/members">
    <img src="https://img.shields.io/github/forks/TypQxQ/kTAMV?style=flat-square">
  </a>
  <a aria-label="License" href="https://github.com/TypQxQ/kTAMV/blob/master/LICENSE">
    <img src="https://img.shields.io/github/license/TypQxQ/kTAMV?style=flat-square">
  </a>
    <a aria-label="License" href="https://github.com/TypQxQ/kTAMV/blob/master/LICENSE">
    <img src="https://img.shields.io/github/license/TypQxQ/kTAMV?style=flat-square">
  </a>
  <a href="https://universe.roboflow.com/nozzle-detection/nozzle-detection-bdk8s">
    <img src="https://app.roboflow.com/images/download-dataset-badge.svg"></img>
  </a>
  <a href="https://universe.roboflow.com/nozzle-detection/nozzle-detection-bdk8s/model/">
    <img src="https://app.roboflow.com/images/try-model-badge.svg"></img>
  </a>
</p>

It has one part that runs as a part of Klipper, adding the necesary commands and integration, and one part that does all the io and cpu intensive calculations as a webserver, localy or on any computer for true multithreading. 

It adds the following commands to klipper:

- `KTAMV_CALIB_CAMERA`, moves the toolhead around the current position for camera-movement data
- `KTAMV_FIND_NOZZLE_CENTER`, detects the nozzle in the current nozzle cam image and attempts to move it to the center of the image.
- `KTAMV_SET_ORIGIN`, sets the current X,Y position as origin to use for calibrating from.
- `KTAMV_GET_OFFSET`, Get the offset from the current X,Y position to the origin X,Y position. Prints it to console.
- `KTAMV_MOVE_TO_ORIGIN`, moves the toolhead to the configured center position origin as set with KTAMV_SET_ORIGIN
- `KTAMV_SIMPLE_NOZZLE_POSITION`, checks if a nozzle is detected in the current nozzle cam image and reports whether it is found. The printer will not move.
- `KTAMV_START_PREVIEW`, starts the camera preview mode.
- `KTAMV_STOP_PREVIEW`, stops the camera preview mode.

!!! !!! !!! !!! !!!
This software is only meant for advanced users!
Please only use while supervising your printer,
may produce unexpected results,
be ready to hit 'emergency stop' at any time!
!!! !!! !!! !!! !!!

## How to install

Connect to your klipper machine using SSH, run these command

```bash
cd ~/ && git clone https://github.com/lrmartins97/NozzleSight.git kTAMV && bash ~/kTAMV/install.sh
```

This will install and configure everything.

## Configuration
The installation script will add a section to printer.cfg that looks like the following:
```yml
[ktamv]
nozzle_cam_url: http://localhost/webcam2/snapshot?max_delay=0
server_url: http://localhost:8085
move_speed: 1800
send_frame_to_cloud: false
detection_tolerance: 0
```
If your nozzle webcamera is on another stream, change that. You can find out what the stream is called in the Mainsail camera configuration. For example, here this is webcam2, so my configuration would be:

`nozzle_cam_url: http://localhost/webcam2/stream`

<img src="doc/mainsail-nozzlecam-settings-example.jpg" width="507">

Change the `server_url` if you run on another machine or port.

`move_speed` is the toolhead spped while calibrating.

`send_frame_to_cloud` indicates if you want to contribute to possible future development of AI based detection.

`detection_tolerance` If the nozzle position is within this many pixels when comparing frames, it's considered a match. Only whole numbers are supported.

## Setting up the server image in Mainsail

Add a webcam and configure it like in the image:
- Any name you like
- URL Stream: Leave Empty
- URL Snapshot: pointing to your server ip on http, not https with the port number it runs on, 8085 as standard.
- Service: Adaptive MJPEG-Streamer
- Target FPS: 4 is enough, will ask the server for a new frame 4 times a second.
Use the printer IP and not localhost or Mainsail will try to connect to the computer you run the webbrowser on.

<img src="doc/mainsail-ktamv-cam-settings-example.jpg" width="689">


----
## How to run

1. Run the `KTAMV_SEND_SERVER_CFG` command to configure the server.
2. Home the printer and move the endstop or nozzle over the camera so that it is aproximatley in the middle of the image. You can run the `KTAMV_START_PREVIEW` command to help you orientate.
2. Run the `KTAMV_CALIB_CAMERA` command to detect the nozzle or endstop. Note that it can have problems with endstops and it's easier to calibrate using a nozzle.
3. If successfull, run the `KTAMV_FIND_NOZZLE_CENTER` command to center the nozzle or endstop.
4. Run the `KTAMV_SET_ORIGIN` command to set this as the origin for all other offsets. If a tool is selected, this should not have any XY offsets applied.
5. Change to another tool and move the nozzle over the camera so that it is aproximatley in the middle of the image. You can run the `KTAMV_START_PREVIEW` command to help you orientate.
6. Run the `KTAMV_FIND_NOZZLE_CENTER` command to center the nozzle.
7. Run the `KTAMV_GET_OFFSET` to get the offset from when the first tool or nozzle was in the middle of the image.
8. Run step 5 - 7 for every tool to get their offset.

## Can it be automated?
Of course! And here is a macro you can use as a start point:
[ktamv_automation_example.cfg](ktamv_automation_example.cfg)

## Debug logs
The kTAMV server logs in memory and everything can be displayed on it's root path.
`http://my_printer_ip_address:8085/`

The Client part logs to regular Klipper logs.

## FAQ
- Why does it not detect my nozzle when not near the center?
  - The further away the nozzle i from the center, the less round will the nozzle look like.
- What can I do to improve the detection? Clean the nozzle so the opening is visible. If you can't see the nozzle cirle, the software won't either. See previous question. Change lightning. Change distance between nozzle and camera.
- Do I need to install the server?
  - Yes.
- Why not run everything inside Klipper?
  - Using a server component moves all io and cpu intensiv work to another cpu core, preventing Klipper to timeout. Also this moves all requirements to the server, not needing to install anything in the Klipper enviroment.
- Why install the requirements to the entire server and not using a venv and pip to install localy?
  - Installing OpenCV, the component doing the Computer Vision magic, takes 2-3 hours to install in a venv because it needs to be compiled in place. Installing it systems wide uses the Raspberry Pi precompiled versions that are tested and maintained by the developers of the OS.
- Can I run the server on another computer?
  - Yes. It runs fine on Windows too.
- Can I run the server in a Docker component?
  - I can't see why not.
- Will anything be sent out from my computer?
  - Only if you allow it with the send_frame_to_cloud option. It will then only send out the unprocessed image, coordinates here it found the middle of the nozzle and what algorithm was used. This is sent to a database and is anonymous.
- Why collect the data?
  - To try to train an AI to find the nozzle. I don't know if and when I can do it but the more data I recieve, the more precise an AI can be with diffrent types of nozzles, heights and lightning setups.
- Why did you build this?
  - I was too lazy too install and run TAMV in a desktop enviroment so I spent weeks on this instead.
- Why do I have to enter the ip adress of the printer and not localhost in my webcam configuration?
  - This is because Mainsail runs in your browser and the address localhost maps to the computer you run the browser on.
- What does the installation script do?
  - It will clone the repository and execute the install script.
The install script will update the system, install the requrements system wide, link the Klipper extensions, add configuration to printer.cfg, add moonraker automatic update, install the server a system.d process and add it to Moonraker to be able to start and stop it within your preffered web interface.

## How it works
This project consists of two parts: a Klipper plugin and a web server based on Flask and Waitress. The Klipper plugin runs within the environment managed by Klipper and does not require any additional components. The web server, on the other hand, depends on various specific components for image recognition, mathematics, statistics and web serving. This project is truly multithreaded because the web server operates in its own Python instance and can even run on a different machine. This is unlike only running in Klipper, which is only multithreaded but does not use multiple cpu cores and has to prioritize real-time interaction with the printer mainboards.

The camera calibration performs small movements around the initial position to keep the nozzle centered and prevent the nozzle opening from becoming oval-shaped. It will try to find the nozzle in each position and calculate the distance in pixels between the two, already knowing the requested physical distance on the printer. It uses ten positions and skips the ones where the nozzle is not detected. It then filters out the values that deviate more than 20% from the average, removing false readings and using only true values. It finallycalculates a matrix it can use to map the distance between a point and the center on the image and the real space coordinates.

When the server needs to find the center of the nozzle it will first fetch a frame from the webcamera, it is the only time it accesses the webcam feed. Then it will resize the image to 640x480 pixels. After this it will try to find a circle that would match the nozzle opening by going trough five diffrent detector and image preprocessor combinations. If it finds multiple circles, it will then use the one closest to the center of the image. It will repeat the above until it has found the same middlepoint 3 consecutive times with a tolerance of one pixel, or it times out, default 20s.

## Special thanks
 - This extension uses much of the logic in TAMV. TAMV uses a GUI inside the Desktop enviroment to align toolheads using computer vision. For more information see: https://github.com/HaythamB/TAMV
- CVToolheadCalibration that is also a Klipper plugin inspired by TAMV but for IDEX printers. For more information see: https://github.com/cawmit/klipper_cv_toolhead_calibration
- The user psyvision from the Jubilee discord, who tested early versions of the extension and gave very valuable feedback
