"""Astra's headless Blender worker.

Runs inside Blender's own Python: either a Python interpreter that can
``import bpy`` (the ``bpy`` wheel on PyPI) or ``blender --background --python``.
It needs nothing but the standard library and bpy, because it is started by
path, never imported from an installed Astra.

It speaks the socket protocol of the Blender MCP add-on - one JSON object per
request, ``{"type": ..., "params": {...}}``, answered with
``{"status": "success", "result": ...}`` or ``{"status": "error", "message": ...}``
- so the same Astra client drives a GUI Blender running the add-on and this
worker. Differences, all deliberate:

* It binds to 127.0.0.1 only, on a port chosen by the OS, and every request
  must carry the per-process token Astra passed in the environment. Another
  local process cannot drive it by guessing the port.
* Commands run on the main thread, in order, because the worker owns it: there
  is no UI loop and no timer.
* ``get_viewport_screenshot`` renders a small preview through the scene camera
  (or a temporary camera framing every visible object), since a background
  Blender has no viewport. Every render setting it touches is restored.
* It exits when Astra closes its stdin, so a crashed or killed Astra never
  leaves a Blender process behind.
"""

import argparse
import contextlib
import io
import json
import math
import os
import socket
import sys
import threading
import time
import traceback

PROTOCOL = "astra-headless/1"
READY = "ASTRA_WORKER_READY"


def parse_arguments(argv):
    # `blender --background --python worker.py -- args` leaves Blender's own
    # arguments in sys.argv; only what follows `--` is ours.
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    parser = argparse.ArgumentParser(prog="astra-headless-worker")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--file", default=None, help=".blend to open instead of the factory scene")
    parser.add_argument("--empty", action="store_true", help="start from an empty scene")
    parser.add_argument("--preview-engine", default="auto")
    return parser.parse_args(argv)


def watch_parent():
    """Exit when Astra goes away. Astra holds our stdin open for our lifetime."""

    def wait():
        try:
            while sys.stdin.buffer.read(1024):
                pass
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def gpu_available():
    """Whether Workbench/EEVEE can render here without killing the process.

    On Linux the bpy module dlopens libEGL when it first needs a GPU context
    and exits outright when it is missing, so this has to be decided before
    rendering, not by catching an exception.
    """
    if not sys.platform.startswith("linux"):
        return True
    import ctypes.util

    return bool(ctypes.util.find_library("EGL"))


class Worker:
    def __init__(self, options):
        import bpy

        self.bpy = bpy
        self.options = options
        engine = (options.preview_engine or "auto").lower()
        if engine == "auto":
            engine = "workbench" if gpu_available() else "cycles"
        self.preview_engine = engine
        prefs = bpy.context.preferences
        # Python embedded in a .blend (text blocks, scripted drivers) must never
        # run: the files this opens can be AI-generated or downloaded.
        try:
            prefs.filepaths.use_scripts_auto_execute = False
        except Exception:
            pass
        if options.file:
            bpy.ops.wm.open_mainfile(filepath=options.file, load_ui=False, use_scripts=False)
        else:
            bpy.ops.wm.read_factory_settings(use_empty=bool(options.empty))
            try:
                bpy.context.preferences.filepaths.use_scripts_auto_execute = False
            except Exception:
                pass

    # -- commands ---------------------------------------------------------

    def ping(self, **_):
        return {"pong": True}

    def get_addon_info(self, **_):
        bpy = self.bpy
        return {
            "name": "Astra headless worker",
            "protocol": PROTOCOL,
            "blender_version": bpy.app.version_string,
            "background": bool(bpy.app.background),
            "preview_engine": self.preview_engine,
            "capabilities": sorted(self.handlers()),
        }

    def get_scene_info(self, **_):
        bpy = self.bpy
        scene = bpy.context.scene
        objects = []
        for obj in list(scene.objects)[:50]:
            objects.append(
                {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [round(float(v), 3) for v in obj.matrix_world.translation],
                }
            )
        return {
            "name": scene.name,
            "file": bpy.data.filepath or None,
            "blender_version": bpy.app.version_string,
            "object_count": len(scene.objects),
            "objects": objects,
            "materials_count": len(bpy.data.materials),
            "frame_range": [scene.frame_start, scene.frame_end],
            "render_engine": scene.render.engine,
        }

    def get_object_info(self, name=None, **_):
        bpy = self.bpy
        obj = bpy.data.objects.get(name or "")
        if obj is None:
            raise ValueError(f"Object not found: {name}")
        info = {
            "name": obj.name,
            "type": obj.type,
            "location": [float(v) for v in obj.location],
            "rotation": [float(v) for v in obj.rotation_euler],
            "scale": [float(v) for v in obj.scale],
            "visible": obj.visible_get(),
            "parent": obj.parent.name if obj.parent else None,
            "materials": [slot.material.name for slot in obj.material_slots if slot.material],
        }
        if obj.type == "MESH" and obj.data is not None:
            info["mesh"] = {
                "vertices": len(obj.data.vertices),
                "edges": len(obj.data.edges),
                "polygons": len(obj.data.polygons),
            }
        return info

    def execute_code(self, code="", **_):
        namespace = {"bpy": self.bpy}
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                exec(compile(code, "<astra>", "exec"), namespace)
        except Exception as error:
            # Same shape as the Blender MCP add-on: the traceback survives the
            # socket hop so the caller can tell which line failed.
            raise RuntimeError(
                json.dumps(
                    {
                        "exception_type": type(error).__name__,
                        "message": str(error),
                        "traceback": traceback.format_exc(),
                        "output": buffer.getvalue()[-4000:],
                    }
                )
            )
        return {"executed": True, "result": buffer.getvalue()}

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png", **_):
        if not filepath:
            raise ValueError("No filepath provided")
        return self.render_preview(filepath, int(max_size or 800))

    def save_as(self, filepath=None, copy=True, **_):
        if not filepath:
            raise ValueError("No filepath provided")
        self.bpy.ops.wm.save_as_mainfile(filepath=filepath, copy=bool(copy))
        return {"saved": filepath}

    def open_file(self, filepath=None, **_):
        if not filepath:
            raise ValueError("No filepath provided")
        self.bpy.ops.wm.open_mainfile(filepath=filepath, load_ui=False, use_scripts=False)
        return {"opened": filepath, "objects": len(self.bpy.context.scene.objects)}

    def handlers(self):
        return {
            "ping": self.ping,
            "get_addon_info": self.get_addon_info,
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "execute_code": self.execute_code,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "save_as": self.save_as,
            "open_file": self.open_file,
        }

    # -- preview rendering --------------------------------------------------

    def render_preview(self, filepath, max_size):
        bpy = self.bpy
        from mathutils import Vector

        scene = bpy.context.scene
        render = scene.render
        settings = {
            "engine": render.engine,
            "resolution_x": render.resolution_x,
            "resolution_y": render.resolution_y,
            "resolution_percentage": render.resolution_percentage,
            "filepath": render.filepath,
            "film_transparent": render.film_transparent,
        }
        image = render.image_settings
        image_settings = {"file_format": image.file_format, "color_mode": image.color_mode}
        cycles = getattr(scene, "cycles", None)
        cycles_settings = {}
        if cycles is not None:
            for name in ("samples", "use_denoising", "device", "use_adaptive_sampling"):
                if hasattr(cycles, name):
                    cycles_settings[name] = getattr(cycles, name)
        original_camera = scene.camera
        temporary = None
        aspect = (
            render.resolution_x * render.pixel_aspect_x / max(1, render.resolution_y * render.pixel_aspect_y)
        )
        try:
            source = "camera"
            if scene.camera is None:
                temporary = self._framing_camera(scene, Vector, aspect)
                scene.camera = temporary
                source = "framing camera (the scene has no camera)"
            if aspect >= 1:
                width, height = max_size, max(16, round(max_size / aspect))
            else:
                width, height = max(16, round(max_size * aspect)), max_size
            render.resolution_x, render.resolution_y = width, height
            render.resolution_percentage = 100
            render.filepath = filepath
            render.film_transparent = False
            image.file_format = "PNG"
            image.color_mode = "RGB"
            engine = self.preview_engine
            if engine == "workbench":
                render.engine = "BLENDER_WORKBENCH"
            elif engine == "eevee":
                render.engine = (
                    "BLENDER_EEVEE_NEXT"
                    if bpy.app.version[:2] in {(4, 2), (4, 3), (4, 4)}
                    else "BLENDER_EEVEE"
                )
            else:
                render.engine = "CYCLES"
                if cycles is not None:
                    cycles.device = "CPU"
                    cycles.samples = 12
                    if hasattr(cycles, "use_denoising"):
                        cycles.use_denoising = False
                    if hasattr(cycles, "use_adaptive_sampling"):
                        cycles.use_adaptive_sampling = True
            started = time.monotonic()
            with contextlib.redirect_stdout(io.StringIO()):
                bpy.ops.render.render(write_still=True)
            return {
                "success": True,
                "filepath": filepath,
                "width": width,
                "height": height,
                "engine": render.engine,
                "source": source,
                "seconds": round(time.monotonic() - started, 2),
                "method": "render",
            }
        finally:
            for name, value in settings.items():
                with contextlib.suppress(Exception):
                    setattr(render, name, value)
            for name, value in image_settings.items():
                with contextlib.suppress(Exception):
                    setattr(image, name, value)
            for name, value in cycles_settings.items():
                with contextlib.suppress(Exception):
                    setattr(cycles, name, value)
            scene.camera = original_camera
            if temporary is not None:
                data = temporary.data
                bpy.data.objects.remove(temporary, do_unlink=True)
                with contextlib.suppress(Exception):
                    bpy.data.cameras.remove(data)

    def _framing_camera(self, scene, Vector, aspect):
        bpy = self.bpy
        points = []
        for obj in scene.objects:
            if obj.type in {"MESH", "CURVE", "SURFACE", "META", "FONT"} and obj.visible_get():
                points.extend(obj.matrix_world @ Vector(corner) for corner in obj.bound_box)
        if points:
            low = Vector([min(p[i] for p in points) for i in range(3)])
            high = Vector([max(p[i] for p in points) for i in range(3)])
        else:
            low, high = Vector((-1, -1, -1)), Vector((1, 1, 1))
        center = (low + high) / 2
        radius = max((high - low).length / 2, 0.1)
        data = bpy.data.cameras.new("ASTRA_PREVIEW_CAMERA")
        data.lens = 50
        camera = bpy.data.objects.new("ASTRA_PREVIEW_CAMERA", data)
        scene.collection.objects.link(camera)
        fov = 2 * math.atan(data.sensor_width / (2 * data.lens))
        if aspect < 1:
            fov *= aspect
        distance = radius / math.sin(fov / 2) * 1.1
        direction = Vector((1.0, -1.2, 0.8)).normalized()
        camera.location = center + direction * distance
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        data.clip_end = max(1000.0, distance * 4)
        bpy.context.view_layer.update()
        return camera

    # -- protocol -------------------------------------------------------------

    def answer(self, request, token):
        if not isinstance(request, dict):
            return {"status": "error", "message": "Request must be a JSON object"}
        if request.get("token") != token:
            return {"status": "error", "message": "Invalid worker token"}
        handler = self.handlers().get(request.get("type"))
        if handler is None:
            return {"status": "error", "message": f"Unknown command type: {request.get('type')}"}
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return {"status": "error", "message": "params must be an object"}
        try:
            return {"status": "success", "result": handler(**params)}
        except Exception as error:
            return {"status": "error", "message": str(error)}


class Reader:
    """Split a byte stream into JSON objects, the way the add-on does.

    Requests are not length-prefixed: a request is complete when it parses.
    Bytes are kept until they decode, because a multi-byte character can be
    split across two reads.
    """

    def __init__(self, client):
        self.client = client
        self.raw = b""
        self.text = ""
        self.decoder = json.JSONDecoder()

    def next(self):
        while True:
            stripped = self.text.lstrip()
            if stripped:
                try:
                    value, end = self.decoder.raw_decode(stripped)
                    self.text = stripped[end:]
                    return value
                except ValueError:
                    pass
            chunk = self.client.recv(1 << 16)
            if not chunk:
                return None
            self.raw += chunk
            try:
                self.text += self.raw.decode("utf-8")
                self.raw = b""
            except UnicodeDecodeError:
                continue


def serve(worker, listener, token):
    while True:
        client, _ = listener.accept()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        reader = Reader(client)
        with client:
            while True:
                try:
                    request = reader.next()
                except OSError:
                    break
                if request is None:
                    break
                if isinstance(request, dict) and request.get("type") == "shutdown":
                    if request.get("token") == token:
                        with contextlib.suppress(OSError):
                            client.sendall(b'{"status": "success", "result": {"bye": true}}')
                        return
                response = worker.answer(request, token)
                try:
                    payload = json.dumps(response, allow_nan=False, default=str)
                except ValueError:
                    payload = json.dumps({"status": "error", "message": "Result is not JSON-serializable"})
                try:
                    client.sendall(payload.encode("utf-8"))
                except OSError:
                    break


def main(argv=None):
    options = parse_arguments(sys.argv if argv is None else argv)
    token = os.environ.pop("ASTRA_WORKER_TOKEN", "")
    if len(token) < 16:
        print("ASTRA_WORKER_ERROR the worker needs ASTRA_WORKER_TOKEN from Astra", flush=True)
        raise SystemExit(2)
    watch_parent()
    try:
        worker = Worker(options)
    except Exception as error:
        print("ASTRA_WORKER_ERROR " + json.dumps(str(error)), flush=True)
        raise SystemExit(1)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", options.port))
    listener.listen(4)
    print(f"{READY} {listener.getsockname()[1]} {worker.bpy.app.version_string}", flush=True)
    try:
        serve(worker, listener, token)
    finally:
        listener.close()


if __name__ == "__main__":
    main()
