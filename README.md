# Tablero de Mercados · Estudio Cisneros

Web propia con dólar, riesgo país, tasas, bolsas, metales, energía y granos.
Se actualiza sola cada 30 minutos en horario de mercado, gratis, sin depender de ninguna computadora.

**Dirección:** https://cisnerosale-maker.github.io/tablero-mercados/

## Cómo funciona

- **GitHub** guarda los archivos y, cada 30 minutos, corre un programa (`scripts/update_data.py`) que busca los datos y los guarda en `data/data.json`.
- **GitHub Pages** publica `index.html` como página web.
- La página lee ese archivo. Los dólares (oficial, MEP, CCL y mayorista) además se refrescan en vivo cada 5 minutos desde el navegador, con el mercado abierto.
- Cada tarjeta tiene un enlace **"Ver fuente"** a la página de donde sale el dato.

## Horarios

| Momento | Qué se muestra |
|---|---|
| Antes de las 10:30 (dólares) o de las 11:00 (riesgo país y Merval) | Cierre del día hábil anterior, con su variación real |
| Durante la rueda | Valor del día, marcado **"en vivo"** (dólares) o **"en curso"** (riesgo país y Merval) |
| Después de las 17:30 | Cierre del día |
| Fines de semana y feriados | Último cierre |

Las actualizaciones automáticas corren cada 30 minutos de lunes a viernes, de 10 a 18:30, y una vez por día los fines de semana. GitHub a veces las demora 20 o 30 minutos en horas de mucho uso.

## Archivos

| Archivo | Para qué sirve | ¿Se edita? |
|---|---|---|
| `index.html` | La página que se ve | Sí, para cambiar diseño, textos o qué tarjetas aparecen |
| `scripts/update_data.py` | El programa que busca los datos | Sí, para agregar o cambiar fuentes |
| `.github/workflows/actualizar.yml` | El horario de las actualizaciones | No hace falta |
| `requirements.txt` | Programas que instala GitHub | No hace falta |
| `data/data.json` | Los datos del último corte | No: lo escribe el programa solo |
| `data/historial.json` | Historia de datos que no tienen fuente histórica (pizarra Rosario, plazo fijo, Merval, riesgo país del día) | No: lo escribe el programa solo |
| `data/manual.json` | Valores de carga manual (hoy sin uso) | Solo si se vuelven a mostrar esas tarjetas |

**Importante:** `update_data.py` va siempre dentro de la carpeta `scripts`. Para subirlo, entrar primero a esa carpeta y recién ahí usar *Add file → Upload files*.

## Fuentes

| Dato | Fuente | Frecuencia |
|---|---|---|
| Dólar oficial, MEP, CCL y mayorista | dolarapi.com (en vivo) y argentinadatos.com (historia) | 5 minutos en la página |
| Brecha CCL / oficial | Cálculo propio | Con los dólares |
| Real brasileño | Banco Central de Brasil (dólar PTAX) | Diaria |
| Peso chileno | Banco Central de Chile (dólar observado, vía mindicador.cl) | Diaria |
| Euro | Banco Central Europeo (tipo de referencia, vía frankfurter) | Diaria |
| Real, peso chileno y euro en pesos | Cálculo propio con el dólar mayorista | Diaria |
| Riesgo país Argentina | Rava Bursátil (dato del día) y argentinadatos.com (historia) | 30 minutos |
| Plazo fijo BNA 30 días | argentinadatos.com (BCRA) | 30 minutos |
| Inflación mensual | argentinadatos.com (INDEC) | Mensual |
| Merval | Rava Bursátil (MSCI Merval) | 30 minutos |
| Merval en dólares | Cálculo propio: Merval / CCL | 30 minutos |
| S&P 500, Nasdaq, Dow Jones, Ibovespa, VIX, índice dólar, bono EE.UU. 10 años | Yahoo Finance | 30 minutos (demora ~15 min) |
| Chile | Yahoo Finance: índice IPSA si está disponible; si no, el ETF ECH en dólares | 30 minutos |
| Cobre, oro, plata, Brent, WTI | Yahoo Finance (futuros) | 30 minutos |
| Soja, maíz, trigo en Chicago | Yahoo Finance (futuros CBOT), convertidos a US$ por tonelada | 30 minutos |
| Pizarra Rosario | Cámara Arbitral de Cereales (Bolsa de Comercio de Rosario) | Diaria |

## Para tener en cuenta

- **Merval:** desde el 19/10/2026 lo calcula MSCI con una base nueva de 100.000 puntos. El viejo S&P Merval cerró en 2.848.945 puntos el 16/10 y se dejó de calcular; algunas plataformas (TradingView, Investing) todavía muestran ese valor congelado. La historia anterior al cambio se muestra reescalada a la base nueva, para que el gráfico y los promedios sean comparables.
- **IPSA:** también pasó a MSCI en octubre de 2026. Mientras Yahoo no publique el índice nuevo, se muestra el ETF de Chile (ECH) en dólares.
- **Petróleo:** cuando Yahoo cambia de contrato de futuros, la variación del día no se muestra porque compara dos contratos distintos. La tarjeta lo aclara.
- **Datos viejos:** si un dato tiene más de 5 días (45 en el caso de la inflación), la tarjeta se marca como **"dato viejo"** con borde punteado.
- **MM50 y MM200:** promedio de las últimas 50 y 200 ruedas. **Prom. año:** promedio desde el 1 de enero.

## Si algo deja de funcionar

- **Un dato muestra "dato viejo":** esa fuente no respondió. Al pie de la página, en *Avisos de la última actualización*, figura el motivo.
- **No se actualiza nada:** en *Actions*, fijarse si la última corrida tiene una cruz roja y abrirla para ver el error.
- **Forzar una actualización:** *Actions → Actualizar datos → Run workflow*.
- **Ver la página nueva después de un cambio:** esperar uno o dos minutos y recargar con Ctrl + F5.
- **GitHub pausa las tareas programadas** de repositorios sin actividad durante 60 días. Como el tablero guarda datos todos los días, no debería pasar; si pasa, en *Actions* aparece un botón para reactivarlas.
- **Rava o la Bolsa de Comercio de Rosario cambian el diseño de su página:** el dato puede dejar de leerse hasta ajustar el programa.

## Agregar o quitar indicadores

- **Quitar una tarjeta o cambiar el orden:** en `index.html`, lista `SECCIONES`.
- **Agregar algo que cotiza en Yahoo Finance:** una línea en la lista `YAHOO` de `scripts/update_data.py` y el código en `SECCIONES` de `index.html`.
- **Agregar un dato de otra fuente:** hace falta código nuevo en `scripts/update_data.py`.

---
Estudio Cisneros · Marcos Juárez · alejandro.estcisneros@gmail.com · +54 3472 589009

Datos de fuentes públicas con hasta 30 minutos de demora. No constituye asesoramiento financiero.
