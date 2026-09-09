# Astra Blender Harness

Un estudio local para conectar modelos de IA al MCP de Blender con tu propia API. Planifica, construye, revisa capturas, refina y guarda copias de la escena.

## Inicio rápido

Necesitas Python 3.11+, Blender con el add-on Blender MCP activo y uv para ejecutar el servidor predeterminado.

El modelo se elige de una lista construida desde el catálogo de LiteLLM instalado, que marca qué modelos admiten llamadas a herramientas y visión. Escribir un nombre comercial en vez del identificador (`deepseek/deepseek-v4-pro`, no `Deepseek V4 Pro`) es la causa habitual de que una ejecución falle en el primer turno. Con el botón **↻** se pregunta al propio proveedor qué modelos sirve y se rellena la lista con identificadores ya listos, lo que resuelve el caso de pasarelas como NVIDIA NIM, cuyos IDs (`deepseek-ai/deepseek-v4-pro-0813`) ningún catálogo estático mantiene.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e '.[keyring]'   # con '.' a secas también funciona, pero no recuerda la clave
astra-blender serve
```

Abre http://127.0.0.1:8765, selecciona proveedor/modelo, introduce tu API y comprueba la conexión con Blender. Usa Demo para explorar sin API ni Blender. El modo demo muestra una ilustración y nunca genera un .blend.

La clave se mantiene en memoria por defecto. Si guardas una configuración, sus datos no secretos (proveedor, modelo, URL base) van a un archivo local y la clave va al llavero del sistema operativo, nunca a ese archivo. Los prompts y las capturas habilitadas se envían al proveedor elegido. Por defecto se pide aprobar las operaciones que modifican Blender; puedes autorizar la ejecución automática para la sesión. El plazo de la ejecución se pausa mientras una aprobación te espera: el tiempo que tardas en revisar no se le descuenta al modelo. Los presupuestos (turnos, tokens, tiempo y el tope de cada fase) se editan desde la interfaz, y un **0** quita ese límite. Recargar la página no pierde nada: el estudio se reengancha a la última ejecución y reconstruye su traza, sus imágenes y sus archivos desde el disco; si sigue en marcha, continúa el seguimiento con las aprobaciones activas. Si una ejecución se queda a medias puedes continuarla: su conversación se guarda y la escena sigue en Blender, así que retoma en la fase donde paró.

Se incluyen recomendaciones para OpenAI, Claude, Gemini y modelos locales en el [README principal](README.md). La compatibilidad depende del protocolo y las capacidades del modelo. Los modelos sin llamadas a herramientas pueden usar acciones JSON; los modelos sin visión reciben información textual de la escena.

Probado con Blender 4.5.9 y Blender MCP 1.9.1 reales. Las pruebas de proveedores no consumen APIs: no se afirma que todos los modelos hayan sido probados con una cuenta de pago.

Complemento: [Blender Quality Lab](https://github.com/AleBrito124356/blender-quality-lab), con escenas reproducibles, inspección técnica y evaluación visual.
