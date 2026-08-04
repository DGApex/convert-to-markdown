# convert-to-markdown

**Cualquier cosa → Markdown, enrutado al motor que de verdad gana en ese formato.**

*[🇬🇧 Read in English](README.md)*

Una skill de [Claude Code](https://claude.com/claude-code) (también usable como CLI a secas). No
existe un único conversor mejor, así que esta no finge lo contrario: inspecciona cada entrada y la
despacha a la herramienta que mejor maneja ese formato, medido.

| Entrada | Motor | Por qué |
|---|---|---|
| `.pdf` | **[pdf-inspector](https://github.com/firecrawl/pdf-inspector)** | Consciente del layout: títulos, multicolumna, tablas. ~20× más rápido |
| `http(s)://` | **[Firecrawl CLI](https://github.com/firecrawl/cli)** | Renderiza SPAs y quita navegación y pies de página |
| docx, pptx, xlsx, epub, msg, csv, json, zip, audio, imágenes | **[MarkItDown](https://github.com/microsoft/markitdown)** | El único que cubre esta cola larga |
| PDF escaneado | → **[super-ocr](../super-ocr)** | pdf-inspector lo detecta pero no hace OCR |

## Créditos — esto es un enrutador, no un motor

| Herramienta | Licencia |
|---|---|
| **[firecrawl/pdf-inspector](https://github.com/firecrawl/pdf-inspector)** | MIT |
| **[microsoft/markitdown](https://github.com/microsoft/markitdown)** | MIT |
| **[firecrawl/cli](https://github.com/firecrawl/cli)** | MIT |
| **[PyMuPDF](https://github.com/pymupdf/PyMuPDF)** | AGPL-3.0 |
| **[astral-sh/uv](https://github.com/astral-sh/uv)** | Apache-2.0/MIT |

## Por qué se le quitaron los PDF a MarkItDown

En [opendataloader-bench](https://github.com/firecrawl/pdf-inspector) (200 PDF, sin OCR)
pdf-inspector puntúa **0,875** global frente al **0,583** de markitdown, con **0,814 vs 0,000** en
tablas (TEDS) y **0,788 vs 0,000** en títulos. Firecrawl publica ese benchmark ellos mismos, así que
se verificó en local sobre arXiv:1706.03762 (15 páginas, dos columnas):

| | pdf-inspector | markitdown |
|---|---|---|
| Títulos detectados | 38 | 0 |
| Párrafos | refluidos correctamente | partidos por las líneas físicas del PDF |
| Celdas de tabla rellenas | 87% | 48% |
| Espaciado de palabras | correcto | pegado (`TheTransformerachieves`) |
| Tiempo | 0,07 s | 1,7 s |

**Pero no es una victoria limpia, y esto importa:** en la Tabla 2 de ese mismo paper pdf-inspector
produjo una grilla hueca (`|||20|`) mientras que markitdown sí recuperó los nombres de modelo y los
BLEU. En páginas visualmente complejas ningún extractor basado en coordenadas es fiable. De ahí
`--pdf-engine both`, y de ahí que el enrutador calcule una **métrica de salud de tablas** y te avise
cuando la extracción sale hueca, en vez de devolverte una grilla vacía en silencio.

## Requisitos

- **[uv](https://github.com/astral-sh/uv)** — el único requisito duro. Las dependencias viven en la
  cabecera [PEP 723](https://peps.python.org/pep-0723/) del script y se resuelven en la caché global
  de uv.
- **Opcional, para URLs:** el [CLI de Firecrawl](https://github.com/firecrawl/cli)
  (`npm install -g firecrawl-cli`) más una cuenta de Firecrawl. Hacer scraping consume créditos.

## Instalación

### Como skill de Claude Code

```bash
git clone https://github.com/<tu-usuario>/convert-to-markdown .claude/skills/convert-to-markdown
```

### Como CLI a secas

```bash
git clone https://github.com/<tu-usuario>/convert-to-markdown
uv run convert-to-markdown/scripts/convert.py informe.pdf
```

Sin `pip install`, sin virtualenv, sin nada en tu Python del sistema.

## Uso

```bash
uv run scripts/convert.py <archivo|carpeta|url ...> [flags]
```

| Flag | Qué hace |
|---|---|
| `--out-dir DIR` | destino (por defecto `converted`) |
| `--recursive` | baja a subcarpetas |
| `--pdf-engine pdf-inspector\|markitdown\|both` | `both` escribe ambos resultados para comparar |
| `--url-engine auto\|firecrawl\|markitdown` | `auto` = Firecrawl si está instalado **y** autenticado |
| `--check-tools` | reporta disponibilidad de motores y sale. **Ejecútalo antes de un lote de URLs** |
| `--overwrite`, `--no-front-matter`, `--enable-plugins` | lo esperable |

La última línea de stdout es legible por máquina:

```
CONVERT_JSON {"converted": 3, "engines": {...}, "encoding_repairs": 265, "firecrawl": {...}, ...}
```

### Ejemplos

```bash
uv run scripts/convert.py informe.pdf
uv run scripts/convert.py docs/ --recursive --out-dir converted
uv run scripts/convert.py https://ejemplo.com/post
uv run scripts/convert.py balance.pdf --pdf-engine both     # comparar motores en una tabla difícil
uv run scripts/convert.py --check-tools                     # comprobación previa
```

## Antes de convertir URLs

```bash
uv run scripts/convert.py --check-tools
```

```
firecrawl CLI : ready  (v1.19.6)  credits 1,000 / 1,000
```

| Resultado | Qué hacer |
|---|---|
| `installed: true, authenticated: true` | adelante |
| `installed: true, authenticated: false` | ejecuta tú mismo `firecrawl auth --api-key fc-…` |
| `installed: false` | `npm install -g firecrawl-cli`, y luego autentícate |

**Por qué la comprobación mira la autenticación y no solo el PATH:** un CLI sin credenciales pasa un
test de `which` y luego falla al hacer scrape, a mitad del lote. Y **no compruebes
`FIRECRAWL_API_KEY`**: el CLI también se autentica con credenciales guardadas por `firecrawl auth`,
así que esa variable está vacía en máquinas que funcionan perfectamente. Lo verificamos exactamente
así: variable sin definir, CLI autenticado, 1.000 créditos. Comprobar la variable habría dado un
falso negativo y mandado al usuario a reinstalar algo que funcionaba.

**Qué está en juego si te lo saltas:** el enrutador cae a MarkItDown para las URLs, que hace un fetch
plano — **sin renderizar JavaScript y conservando navegación y pies de página**. En una SPA eso es
una página casi vacía. La degradación ya no es silenciosa (avisa por archivo, y el motivo queda en
`warnings` de cada registro), pero preguntar antes es mejor que explicarlo después.

> Nunca pegues una clave de API en un chat, ni dejes que un agente ejecute `firecrawl auth` con tu
> clave en tu nombre. Es una credencial: la escribes tú en tu propia terminal.

## PDF en español (o con cualquier acento): la reparación que no se ve trabajar

pdf-inspector **resuelve mal las fuentes Type1 subseteadas** (`enc=T1_x`), extremadamente comunes en
documentos de diseño. O borra el glifo acentuado —`Producción` → `Produccin`— o emite el acento
suelto —`áreas` → `Æreas`, `técnico` → `tØcnico`—. Su propia bandera `has_encoding_issues` se queda
en **`false`**, así que nada te avisa, y el daño se lee como una errata en vez de como un bug.

Medido en un documento español de 41 páginas: **156 defectos, en silencio**. PyMuPDF lee esas mismas
fuentes correctamente, así que se usa como referencia, y el conteo se reporta como
`encoding_repairs`. En ese documento: **156 → 7**.

Las sustituciones solo ocurren cuando la referencia ofrece un único candidato inequívoco. Los que
quedan son palabras cuyo acento cae en la primera letra (`Áreas` vs `áreas`), donde el acento se
lleva consigo la pista de mayúscula. La referencia aquí es **el documento entero**, más ambigua que
la referencia por página que usa [super-ocr](../super-ocr) — por eso el mismo archivo deja 7
residuales aquí y 1 allá.

**Si conviertes PDF que no están en inglés, esto es lo más valioso de este repositorio.**

## Otras cosas que conviene saber

- **Fuerza UTF-8 en stdout.** El script imprime `→` y `·`. Bajo una consola cp1252 convierte todo
  bien y luego muere en la línea del resumen, llevándose por delante el contrato `CONVERT_JSON`:
  trabajo hecho, resultado perdido. Arreglado aquí; merece la pena copiarlo a tus propias
  herramientas.
- **La salida de un subproceso también necesita codificación explícita.** La primera versión del
  chequeo de Firecrawl reportaba `NOT authenticated` sobre un CLI perfectamente autenticado, porque
  `subprocess` decodificaba en cp1252 la salida con emojis del CLI y lanzaba excepción. Un chequeo
  que miente es peor que no tener chequeo.
- **No uses `markitdown[all]`.** Arrastra los extras de Azure, y `azure-ai-contentunderstanding` solo
  existe como pre-release, que `uv` rechaza por defecto (pip no) — falla toda la resolución. La
  cabecera PEP 723 fija exactamente los extras que este enrutador delega.
- **Los PDF escaneados se detectan, no se convierten.** El enrutador reporta `needs_ocr` y te manda a
  [super-ocr](../super-ocr).

## Licencia

MIT — ver [LICENSE](LICENSE), incluidas las notas sobre el estado AGPL-3.0 de PyMuPDF y sobre el
hecho de que Firecrawl es un servicio de terceros de pago.
