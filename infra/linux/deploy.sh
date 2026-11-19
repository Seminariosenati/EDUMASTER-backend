#!/usr/bin/env bash
# Despliega EduMaster en Ubuntu Server con Docker. Uso:
#   curl -fsSL https://raw.githubusercontent.com/Seminariosenati/edumaster-backend/main/infra/linux/deploy.sh | bash
# o, ya clonado: bash infra/linux/deploy.sh
set -euo pipefail

BASE=${BASE:-/opt/edumaster}
BACKEND_REPO=${BACKEND_REPO:-https://github.com/Seminariosenati/edumaster-backend.git}
FRONTEND_REPO=${FRONTEND_REPO:-https://github.com/Seminariosenati/edumaster-frontend.git}
DOMAIN=${DOMAIN:-edumaster-linux.northcentralus.cloudapp.azure.com}

command -v docker >/dev/null || { curl -fsSL https://get.docker.com | sudo sh; }
command -v git >/dev/null || sudo apt-get install -y git

sudo mkdir -p "$BASE" && sudo chown "$USER" "$BASE"
for pair in "edumaster-backend $BACKEND_REPO" "edumaster-frontend $FRONTEND_REPO"; do
  set -- $pair
  if [ -d "$BASE/$1/.git" ]; then git -C "$BASE/$1" pull --ff-only; else git clone "$2" "$BASE/$1"; fi
done

cd "$BASE/edumaster-backend"
if [ ! -f .env ]; then
  cat > .env <<ENV
DB_PASSWORD=$(openssl rand -hex 16)
JWT_SECRET=$(openssl rand -hex 32)
SITE_ADDRESS=${DOMAIN}
SEED_DEMO=true
FRONTEND_DIR=../edumaster-frontend
ENV
  chmod 600 .env
  echo "Se creó .env con secretos aleatorios (dominio: ${DOMAIN})"
fi

# IP pública desde el servicio de metadatos de Azure, para permitir también el acceso por http://<IP>.
if ! grep -q '^PUBLIC_IP=' .env; then
  IP=$(curl -s -m 5 -H Metadata:true "http://169.254.169.254/metadata/instance/network/interface/0/ipv4/ipAddress/0/publicIpAddress?api-version=2021-02-01&format=text" || true)
  if [ -n "$IP" ]; then echo "PUBLIC_IP=$IP" >> .env; echo "Acceso por IP habilitado: http://$IP"; fi
fi

# El contenedor nginx de prueba ocupa el puerto 80.
if sudo docker ps -a --format '{{.Names}}' | grep -qx prueba; then
  echo "Deteniendo el contenedor de prueba 'prueba' (nginx) para liberar el puerto 80"
  sudo docker rm -f prueba
fi

sudo docker compose up -d --build
echo "Listo. Abre https://$(grep SITE_ADDRESS .env | cut -d= -f2)  (el primer arranque entrena el modelo DEMO en ~10 s)"
