#!/bin/bash
# ============================================================================
# nginx_proxy.sh - poe o servidor do NozzleSight acessivel em /ktamv/
# ============================================================================
# O servidor de visao corre na porta 8085. Sem isto, a camara do NozzleSight
# no Mainsail tem de levar o IP da maquina (http://<IP>:8085/image) e deixa de
# funcionar quando a maquina muda de rede. Com isto, o nginx do Mainsail
# encaminha /ktamv/ para a porta 8085 e o endereco da camara passa a ser so':
#     URL Snapshot:  /ktamv/image
# (igual em todas as maquinas e em qualquer rede, como as outras camaras).
#
# Mexe no ficheiro do site do Mainsail que o KIAUH cria. Antes de mexer:
#   - se /ktamv/ ja existir, nao faz nada;
#   - guarda uma copia (sites-available/<site>.antes-nozzlesight);
#   - testa a configuracao (nginx -t) e, se falhar, repoe a copia.
# Uma reinstalacao do Mainsail pelo KIAUH pode reescrever o ficheiro: nesse
# caso, corre este script outra vez.
#
# Uso:  bash nginx_proxy.sh            (site "mainsail", porta 8085)
#       SITE_NAME=fluidd PORT=8085 bash nginx_proxy.sh
# ============================================================================
set -euo pipefail

SITE_NAME="${SITE_NAME:-mainsail}"
PORT="${PORT:-8085}"
NGINX_DIR="${NGINX_DIR:-/etc/nginx}"
SUDO="${SUDO-sudo}"

enabled="${NGINX_DIR}/sites-enabled/${SITE_NAME}"
if [ ! -e "${enabled}" ]; then
    echo "nginx: nao encontrei ${enabled} (o ${SITE_NAME} foi instalado pelo KIAUH?). Nada alterado."
    exit 1
fi
# Edita o ficheiro real (normalmente sites-available/<site>, ligado por atalho)
site="$(readlink -f "${enabled}")"

if grep -q 'location /ktamv/' "${site}"; then
    echo "nginx: /ktamv/ ja esta configurado em ${site}. Nada alterado."
    exit 0
fi

tmp="$(mktemp)"
trap 'rm -f "${tmp}"' EXIT
# Insere o bloco antes de "location /webcam/ {"; se nao houver, antes da
# ultima chaveta do ficheiro (fim do bloco server).
awk -v port="${PORT}" '
    { line[NR] = $0 }
    END {
        at = 0
        for (i = 1; i <= NR; i++) if (line[i] ~ /^[ \t]*location \/webcam\/[ \t]*\{/) { at = i; break }
        if (!at) for (i = NR; i >= 1; i--) if (line[i] ~ /^[ \t]*\}[ \t]*$/) { at = i; break }
        if (!at) exit 3
        for (i = 1; i <= NR; i++) {
            if (i == at) {
                print "    # NozzleSight: imagem do servidor de visao (porta " port ") em /ktamv/"
                print "    location /ktamv/ {"
                print "        proxy_pass http://127.0.0.1:" port "/;"
                print "        proxy_buffering off;"
                print "        access_log off;"
                print "    }"
                print ""
            }
            print line[i]
        }
    }' "${site}" > "${tmp}" || { echo "nginx: formato de ${site} desconhecido. Nada alterado."; exit 1; }

backup="${NGINX_DIR}/sites-available/${SITE_NAME}.antes-nozzlesight"
${SUDO} cp -p "${site}" "${backup}"
${SUDO} cp "${tmp}" "${site}"
if ${SUDO} nginx -t >/dev/null 2>&1; then
    ${SUDO} systemctl reload nginx
    echo "nginx: /ktamv/ configurado. Copia do original: ${backup}"
    echo "No Mainsail, na camara do NozzleSight:  URL Snapshot = /ktamv/image"
else
    ${SUDO} cp -p "${backup}" "${site}"
    echo "nginx: a configuracao nova falhou o teste (nginx -t). Original reposto; nada mudou."
    exit 1
fi
