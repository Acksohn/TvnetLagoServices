#!/usr/bin/env bash
# Actualiza el panel con lo último de GitHub.
# Uso (como usuario tvnet):   bash /srv/tvnetlago/deploy/actualizar.sh
set -e
cd /srv/tvnetlago

echo "==> Bajando cambios de GitHub"
git pull

echo "==> Instalando librerías nuevas (si hay)"
venv/bin/pip install -r requirements.txt

echo "==> Aplicando migraciones"
venv/bin/python manage.py migrate

echo "==> Juntando archivos estáticos"
venv/bin/python manage.py collectstatic --noinput

echo "==> Reiniciando panel y workers"
sudo systemctl restart tvnetlago-web
sudo systemctl restart 'celery@*'

echo "==> Listo"
systemctl --no-pager --type=service --state=running | grep -E 'tvnetlago|celery@' || true
