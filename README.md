# Tablero de Mercados

Web propia con dólar, riesgo país, tasas, bolsas, metales, energía y granos.
Se actualiza sola cada 30 minutos en horario de mercado, gratis, sin depender de tu computadora.

## Cómo funciona

- **GitHub** guarda los archivos y, cada 30 minutos, corre un programa (`scripts/update_data.py`) que busca los datos y los guarda en `data/data.json`.
- **GitHub Pages** publica `index.html` como una página web con tu propia dirección, por ejemplo `https://tuusuario.github.io/tablero-mercados/`.
- La página lee ese archivo. Los dólares (oficial, MEP, CCL) además se refrescan en vivo cada 5 minutos desde el navegador.

## Publicarla (una sola vez, unos 15 minutos)

1. **Creá una cuenta en GitHub**, gratis, en https://github.com/signup.
2. **Creá un repositorio nuevo:** botón verde *New* → nombre `tablero-mercados` → marcá **Public** → *Create repository*.
   (Tiene que ser público para que GitHub Pages sea gratis. Solo contiene datos de mercado públicos.)
3. **Subí los archivos:** en el repositorio vacío, hacé clic en *uploading an existing file*, arrastrá **todo el contenido** de la carpeta descomprimida (incluida la carpeta `.github`) y tocá *Commit changes*.
   - Si la carpeta `.github` no aparece (en Mac y Windows está oculta): *Add file* → *Create new file*, escribí como nombre `.github/workflows/actualizar.yml`, pegá el contenido de ese archivo y guardá.
4. **Activá la página:** *Settings* → *Pages* → en *Branch* elegí `main` y carpeta `/ (root)` → *Save*. En un par de minutos aparece arriba la dirección de tu web.
5. **Habilitá la actualización automática:** pestaña *Actions* → si pide confirmación, *I understand my workflows, go ahead and enable them*.
6. **Corré la primera actualización a mano:** *Actions* → *Actualizar datos* → *Run workflow*. Tarda 1 o 2 minutos. Después de eso, sigue sola.

Guardá la dirección como favorito en el celular y en la computadora.

## Datos que se cargan a mano

Riesgo país de Brasil y Chile, y la tasa Selic, no tienen una fuente gratuita confiable.
Para cargarlos: abrí `data/manual.json` en GitHub → ícono del lápiz → cambiá `valor` y `fecha` → *Commit changes*.
Aparecen en la próxima actualización.

## Fuentes

| Dato | Fuente | Frecuencia |
|---|---|---|
| Dólar oficial, MEP, CCL, mayorista | dolarapi.com (en vivo) y argentinadatos.com (historia) | 5 min en la página |
| Real y peso chileno | Yahoo Finance y BNA vía dolarapi.com | 30 min |
| Riesgo país Argentina | argentinadatos.com (EMBI) | 30 min |
| Plazo fijo BNA 30 días | argentinadatos.com (BCRA) | 30 min |
| Inflación mensual | argentinadatos.com (INDEC) | Mensual |
| Merval, S&P 500, Nasdaq, Dow, Ibovespa, IPSA, VIX, DXY, bono EE.UU. 10 años | Yahoo Finance | 30 min (demora ~15 min) |
| Cobre, oro, plata, Brent, WTI | Yahoo Finance (futuros) | 30 min |
| Soja, maíz, trigo en Chicago | Yahoo Finance (futuros CBOT) | 30 min |
| Pizarra Rosario | Cámara Arbitral de Cereales (BCR) | Diaria |
| Merval en dólares | Cálculo: Merval / CCL | 30 min |

## Si algo deja de funcionar

- **Un dato muestra "último dato":** esa fuente no respondió en la última corrida; el resto sigue andando. Al pie de la página, en *Avisos*, figura el motivo.
- **No se actualiza nada:** en *Actions* fijate si la última corrida tiene una cruz roja y abrila para ver el error.
- **GitHub pausa las tareas programadas** de repositorios sin actividad por 60 días. Como el tablero guarda datos todos los días, no debería pasar; si pasa, en *Actions* aparece un botón para reactivarlas.
- **La pizarra de Rosario** se lee de la página de la Cámara Arbitral. Si cambian el diseño de esa página, puede dejar de leerse hasta ajustar el programa.

## Agregar o quitar indicadores

Los que vienen de Yahoo Finance se configuran en `scripts/update_data.py`, en la lista `YAHOO` (código, nombre, unidad).
En `index.html`, la lista `SECCIONES` define qué tarjetas se ven y en qué orden.
