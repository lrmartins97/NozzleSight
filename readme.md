# NozzleSight

Calibração entre ferramentas para impressoras Klipper com várias cabeças (IDEX).
Mede com uma câmara virada para cima a posição X/Y de cada bico e, com o endstop Z,
a diferença de altura entre bicos. Grava os offsets da segunda ferramenta em relação
à primeira e impede imprimir com as duas cabeças sem calibração válida.

Foi desenvolvido para a impressora M-BIND (seringas, dois bicos) e só está testado nela.

**Limites:**
- Os comandos `KTAMV_*`, o servidor e o detetor funcionam em qualquer impressora Klipper
  com uma câmara virada para cima, como no kTAMV.
- A calibração entre ferramentas (`idex_z_offset`, `idex_camera_focus`) depende das macros
  da configuração da M-BIND, que não estão neste repositório. São elas que limpam os bicos,
  ligam a luz, correm o fluxo completo (`CALIBRATE_IDEX`) e aplicam os offsets gravados ao
  trocar de ferramenta (`T0`/`T1`). Sem essas macros, os offsets são medidos e gravados,
  mas a impressão não os usa.
- Suporta duas ferramentas (offsets do T1 em relação ao T0).

## Agradecimento

O NozzleSight é um fork do **[kTAMV](https://github.com/TypQxQ/kTAMV)**, criado por
**Andrei Ignat ([TypQxQ](https://github.com/TypQxQ))**. A ligação ao Klipper, o servidor
de visão e o método de calibração da câmara são trabalho dele, e sem isso este projeto
não existia. Obrigado, Andrei, por o teres publicado em código aberto.

Licença: GPL v3, a mesma do kTAMV (ver `LICENSE`). A documentação original está no
[repositório do kTAMV](https://github.com/TypQxQ/kTAMV); alguns comandos e passos de lá
não se aplicam a este fork.

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
- acrescenta `[update_manager nozzlesight]` ao `moonraker.conf`, se ainda não existir (também reconhece o nome antigo `ktamv`, para não duplicar);
- acrescenta `[ktamv]` e as macros à configuração do Klipper **só se ainda não existirem**
  em nenhum ficheiro da configuração (procura também nos ficheiros incluídos), e escreve-as
  antes do bloco do SAVE_CONFIG. Faz cópia dos ficheiros que altera.

Pode ser corrido outra vez numa máquina já instalada: funciona como atualização.
Funciona em Debian 11, 12 e 13 (Raspberry Pi OS).

Atualizar: no Mainsail, Máquina → Atualizações → `nozzlesight`.

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
