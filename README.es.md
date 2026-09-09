# Astra Blender Harness

Un estudio local para conectar modelos de IA al MCP de Blender con tu propia API. Planifica, construye, revisa capturas, refina y guarda copias de la escena.

## Inicio rápido

Necesitas Python 3.11+, Blender con el add-on Blender MCP activo y uv para ejecutar el servidor predeterminado.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .
astra-blender serve
```

Abre http://127.0.0.1:8765, selecciona proveedor/modelo, introduce tu API y comprueba la conexión con Blender. Usa Demo para explorar sin API ni Blender. El modo demo muestra una ilustración y nunca genera un .blend.

La clave se mantiene en memoria. Los prompts y las capturas habilitadas se envían al proveedor elegido. Por defecto se pide aprobar las operaciones que modifican Blender; puedes autorizar la ejecución automática para la sesión.

Se incluyen recomendaciones para OpenAI, Claude, Gemini y modelos locales en el [README principal](README.md). La compatibilidad depende del protocolo y las capacidades del modelo. Los modelos sin llamadas a herramientas pueden usar acciones JSON; los modelos sin visión reciben información textual de la escena.

Probado con Blender 4.5.9 y Blender MCP 1.9.1 reales. Las pruebas de proveedores no consumen APIs: no se afirma que todos los modelos hayan sido probados con una cuenta de pago.

Complemento: [Blender Quality Lab](https://github.com/AleBrito124356/blender-quality-lab), con escenas reproducibles, inspección técnica y evaluación visual.
