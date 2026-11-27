#!/usr/bin/env bash
# Actualización en ciclo durante el horario de mercado.
# GitHub no garantiza los horarios programados (los "cada 30 minutos" se pierden seguido), así que
# cada ejecución se queda activa y actualiza cada 15 minutos hasta las 18:40 (hora argentina)
# o hasta casi el tope de 6 horas de GitHub. La siguiente ejecución programada continúa.
#
# Uso: scripts/ciclo.sh "<comando>" "<prefijo del commit>" <rutas a guardar...>
# Con UNA_VEZ=1 hace una sola vuelta (ejecuciones manuales o por cambios en archivos).
set -u
CMD="$1"; PREF="$2"; shift 2
RUTAS=("$@")
LIMITE=$(( $(date +%s) + 335 * 60 ))
TZ_AR=America/Argentina/Buenos_Aires

git config user.name "tablero-bot"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

while true; do
  bash -c "$CMD" || echo "::warning::Falló una vuelta de actualización; se reintenta en la próxima"
  git add "${RUTAS[@]}"
  if git diff --cached --quiet; then
    echo "Sin cambios"
  else
    git commit -q -m "$PREF $(TZ=$TZ_AR date +'%d/%m %H:%M')"
    for i in 1 2 3; do
      git pull -q --rebase --autostash && git push -q && break
      sleep 10
    done
  fi
  if [ "${UNA_VEZ:-0}" = "1" ]; then break; fi
  HM=$(TZ=$TZ_AR date +%H%M); DOW=$(TZ=$TZ_AR date +%u)
  if [ "$DOW" -gt 5 ] || [ $((10#$HM)) -lt 930 ] || [ $((10#$HM)) -ge 1840 ]; then
    echo "Fuera del horario de mercado: termina."
    break
  fi
  if [ $(( $(date +%s) + 900 )) -gt "$LIMITE" ]; then
    echo "Cerca del tope de tiempo de GitHub: sigue la próxima ejecución programada."
    break
  fi
  sleep 900
  git pull -q --rebase --autostash || true
done
