# Astra Blender Harness

Un estudio local para conectar modelos de IA al MCP de Blender con tu propia API. Planifica, construye, inspecciona la escena con datos reales, refina y guarda copias. La interfaz muestra geometría 3D interactiva sincronizada con Blender.

## Nuevo en 0.2

- Vista 3D grande: gira, acerca, selecciona y enfoca objetos; alterna rejilla, alambre y cámara de render. La navegación no modifica Blender. La geometría se actualiza aproximadamente cada dos segundos cuando Blender está libre.
- Los modelos sin visión reciben dimensiones, límites, encuadre, materiales y posibles objetos colocados dentro de otros. No se piden capturas ni se envían imágenes cuando desactivas Vision feedback.
- Los modelos con visión reciben además PNG del viewport. La vista humana conserva su posición mientras el modelo trabaja.
- Herramienta de encuadre por nombres de objetos, márgenes y cámara perspectiva/ortográfica. Los controles de calidad quedan en `quality.json`; también se inspeccionan cambios parciales tras errores de Python.
- Presupuestos con turnos reservados para terminar, avisos al modelo antes de agotarlos y reparación de respuestas vacías. Historial de ejecuciones y continuación tras cierres inesperados, sin repetir operaciones de resultado desconocido.
- Se conservan las configuraciones guardadas, el llavero del sistema, la búsqueda de modelos y la ampliación del visor. Corregidos los IDs personalizados al recargar y el uso local sin clave.

El visor usa geometría evaluada real y materiales simplificados; no reproduce texturas, luces o composición final de Cycles/Eevee. Durante un render conserva la última escena navegable hasta que Blender pueda responder. Consulta [detalles, límites y desarrollo](docs/live-scene.md).

## Qué puede hacer el modelo

Además de las herramientas del MCP de Blender (leer la escena y sus objetos, capturar el viewport en ejecuciones con visión y ejecutar Python con aprobación), Astra ofrece herramientas de confianza cuyo código no escribe el modelo: sus argumentos se validan contra un esquema y se inyectan como literales. `astra_inspect_scene` (evidencia numérica, solo lectura), `astra_frame_camera` (encuadre de sujetos concretos), `astra_assemble_parts` (ensamblar piezas bajo un Empty sin moverlas), `astra_place_on_ground` (apoyar un conjunto entero en el suelo), `astra_keyframe_object` (keyframes locales, en radianes) y `astra_inspect_animation` (poses evaluadas en hasta cinco frames más un barrido de cajas en dieciséis que detecta solapamientos nuevos, piezas bajo el suelo y piezas separadas de su ancla). Las fases de planificación y revisión solo ofrecen las de lectura.

## Vista previa de movimiento

El timeline del visor hornea las matrices de la animación en una sola lectura y la reproduce en el navegador: play, pausa, bucle, velocidad ½×/2×, scrub instantáneo y espacio para alternar. No mueve el playhead de Blender ni le pide un frame cada vez, y funciona mientras el modelo trabaja. Las mallas que deforman (armaduras, shape keys, simulaciones) conservan su pose actual y se indican. El timeline se ve antes de que haya keyframes, deshabilitado y con el motivo. La sincronización en vivo es incremental: Blender solo serializa las mallas que cambiaron desde la última lectura.

## Resultados de una ejecución

Un identificador de modelo que LiteLLM no sabe enrutar se rechaza **antes** de arrancar, diciendo cuál usar: lo que muestra la página de un proveedor (`moonshotai/kimi-k3`) necesita el proveedor delante (`nvidia_nim/moonshotai/kimi-k3`) o una URL base. Si el proveedor falla, el mensaje nombra su código HTTP y qué hacer —un modelo retirado se lee como 410—; su cuerpo no entra en la traza y queda en `error.log`.

`completed`, `incomplete` (la escena está construida y guardada pero falta un entregable pedido —por ahora, una animación sin keyframes— y se puede continuar), `budget_exhausted`, `cancelled` y `failed`. Las palabras de movimiento en el brief son solo una pista: únicamente «Animate» exige keyframes. Cada ejecución guarda en `runs/<id>/` copias `.blend` por fase (nunca sobrescribe tu archivo), capturas, `quality.json`, `animation.json`, las fotos de referencia normalizadas, `state.json` para continuar, la traza `events.jsonl` con la clave redactada, `manifest.json` y `error.log` si falló.

## Inicio rápido

Necesitas Python 3.11+, Blender con el add-on Blender MCP activo y, opcionalmente, uv: si `uvx` no está en el PATH, Astra usa el `blender-mcp` instalado junto a su propio intérprete.

El modelo se elige de una lista construida desde el catálogo de LiteLLM instalado, que marca qué modelos admiten llamadas a herramientas y visión. Escribir un nombre comercial en vez del identificador (`deepseek/deepseek-v4-pro`, no `Deepseek V4 Pro`) es la causa habitual de que una ejecución falle en el primer turno. Con el botón **↻** se pregunta al propio proveedor qué modelos sirve y se rellena la lista con identificadores ya listos, lo que resuelve el caso de pasarelas como NVIDIA NIM, cuyos IDs (`deepseek-ai/deepseek-v4-pro-0813`) ningún catálogo estático mantiene.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e '.[keyring]'   # con '.' a secas también funciona, pero no recuerda la clave
astra-blender serve
```

Abre http://127.0.0.1:8765, selecciona proveedor/modelo, introduce tu API y comprueba la conexión con Blender. Usa Demo para explorar sin API ni Blender. El modo demo muestra una ilustración y nunca genera un .blend.

La clave se mantiene en memoria por defecto. Si guardas una configuración, sus datos no secretos (proveedor, modelo, URL base) van a un archivo local y la clave va al llavero del sistema operativo, nunca a ese archivo. Los prompts y las capturas habilitadas se envían al proveedor elegido. Por defecto se pide aprobar las operaciones que modifican Blender; puedes autorizar la ejecución automática para la sesión. El plazo de la ejecución se pausa mientras una aprobación te espera: el tiempo que tardas en revisar no se le descuenta al modelo. Los presupuestos (turnos, tokens, tiempo y el tope de cada fase) se editan desde la interfaz, y un **0** quita ese límite. El tiempo máximo de *un turno* del modelo también es ajustable (300 s por defecto): los modelos de razonamiento piensan minutos antes del primer token, y un turno que expira no se reintenta, así que una generación lenta no se cobra dos veces. Recargar la página no pierde nada: el estudio se reengancha a la última ejecución y reconstruye su traza, sus imágenes y sus archivos desde el disco; si sigue en marcha, continúa el seguimiento con las aprobaciones activas. Si una ejecución se queda a medias puedes continuarla: su conversación se guarda y retoma la fase donde paró tras inspeccionar Blender. Mantén abierta la escena correspondiente: continuar no carga automáticamente un archivo .blend.

Se incluyen recomendaciones para OpenAI, Claude, Gemini y modelos locales en el [README principal](README.md). La compatibilidad depende del protocolo y las capacidades del modelo. Los modelos sin llamadas a herramientas pueden usar acciones JSON; los modelos sin visión reciben información textual de la escena.

Probado con Blender 4.5.9, Blender 5.2.1 y Blender MCP 1.9.1 reales. Las pruebas de proveedores no consumen APIs: no se afirma que todos los modelos hayan sido probados con una cuenta de pago.

Complemento: [Blender Quality Lab](https://github.com/AleBrito124356/blender-quality-lab), con escenas reproducibles, inspección técnica y evaluación visual.
