import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic import Field as PydField
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, profiles
from .bridge import connect, discover
from .config import MCPConfig, RunConfig, validate_api_base
from .engine import TERMINAL, Run, execute, result_parts

RUN_ID = re.compile(r"^[0-9a-f]{32}$")


class ArchivedRun:
    """A run rebuilt from its own files.

    Everything the studio shows lives in the browser's DOM, so a reload used to
    throw the trace, the viewport gallery and the deliverables away even though
    they were all still on disk. Reading them back means a refresh - or a
    restart of Astra - costs nothing.
    """

    def __init__(self, run_id, directory):
        self.id = run_id
        self.directory = directory
        self.approvals = {}
        self.task = None
        manifest = _read_json(directory / "manifest.json") or {}
        self.status = manifest.get("status", "unknown")
        self.steps = manifest.get("steps", 0)
        self.total_tokens = manifest.get("total_tokens", 0)
        self.events = []
        try:
            for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass

    def snapshot(self, after=0):
        return {
            "id": self.id,
            "status": self.status,
            "steps": self.steps,
            "total_tokens": self.total_tokens,
            "events": self.events[after:],
            "cursor": len(self.events),
            "archived": True,
        }


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def create_app(config=None, output=None):
    config = config or MCPConfig()
    output = Path(output or "runs").resolve()
    token = secrets.token_urlsafe(32)
    runs = {}
    busy = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        tasks = [r.task for r in runs.values() if r.task and not r.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Astra Blender Harness", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.runs = runs
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]"])

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Cross-site requests are disabled"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default error body can echo the API key from invalid input.
        return JSONResponse(
            {"detail": "Invalid request. Check field lengths, limits and endpoint URL."}, status_code=422
        )

    async def auth(x_astra_token: str = Header(default="")):
        if not secrets.compare_digest(x_astra_token, token):
            raise HTTPException(401, "Invalid session token")

    def get_run(run_id):
        if run_id in runs:
            return runs[run_id]
        # Fall back to the files on disk. The id shape is checked first, so this
        # cannot be pointed outside the output directory.
        directory = output / run_id
        if RUN_ID.match(run_id) and (directory / "events.jsonl").is_file():
            return ArchivedRun(run_id, directory)
        raise HTTPException(404, "No run with that id in this process or in runs/")

    @app.get("/api/session")
    async def session():
        return {"token": token, "transport": config.transport, "version": "0.1.0"}

    @app.post("/api/doctor", dependencies=[Depends(auth)])
    async def doctor():
        if busy.locked():
            raise HTTPException(409, "A Blender connection is already active")
        async with busy:
            try:
                async with asyncio.timeout(30):
                    async with connect(config) as client:
                        tools = await discover(client, config.allowed_tools)
                        arguments = (
                            {"user_prompt": "Check Blender connection"}
                            if "user_prompt" in tools["get_scene_info"].inputSchema.get("properties", {})
                            else {}
                        )
                        result = await client.call_tool("get_scene_info", arguments)
                        if result_parts(result)[0].startswith("TOOL ERROR:"):
                            raise RuntimeError("Blender unavailable")
                        return {"ok": True, "tools": list(tools)}
            except Exception:
                raise HTTPException(
                    503, "Cannot reach Blender. Check the MCP command, add-on and its Start button."
                )

    @app.get("/api/models", dependencies=[Depends(auth)])
    async def model_catalog():
        # Read from the installed LiteLLM catalog rather than a hand-kept list,
        # so a wrong identifier fails in the picker instead of mid-run.
        return {"providers": await asyncio.to_thread(catalog.models)}

    class Discovery(BaseModel):
        model_config = ConfigDict(extra="forbid")
        provider: str = PydField(default="custom", max_length=40)
        api_base: str | None = PydField(default=None, max_length=300)
        api_key: SecretStr = SecretStr("")
        profile: str | None = PydField(default=None, max_length=40)

    @app.post("/api/models/discover", dependencies=[Depends(auth)])
    async def discover_models(body: Discovery):
        # Ask the provider what it actually serves. Same URL rules as a run, so
        # this cannot be pointed at an arbitrary internal host.
        try:
            base = validate_api_base(body.api_base)
        except ValueError as error:
            raise HTTPException(400, str(error))
        key = body.api_key.get_secret_value()
        if not key and body.profile:
            key = profiles.load_secret(body.profile) or ""
        try:
            found = await catalog.discover(body.provider, base, key)
        except ValueError as error:
            raise HTTPException(400, str(error))
        except Exception:
            # Never surface a client exception body: it can echo the key.
            raise HTTPException(502, "Model discovery failed. Check the API base URL and key.")
        return {"models": found}

    @app.get("/api/profiles", dependencies=[Depends(auth)])
    async def list_profiles():
        return {"profiles": profiles.listing(), "secret_backend": profiles.secret_backend()}

    class ProfileSave(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str = PydField(min_length=1, max_length=40)
        provider: str = PydField(default="custom", max_length=40)
        model: str = PydField(min_length=1, max_length=200)
        api_base: str | None = PydField(default=None, max_length=300)
        tool_mode: str = PydField(default="native", max_length=10)
        vision: bool = True
        api_key: SecretStr = SecretStr("")

    @app.put("/api/profiles", dependencies=[Depends(auth)])
    async def save_profile(body: ProfileSave):
        profile = profiles.Profile(**body.model_dump(exclude={"api_key"}))
        try:
            return profiles.save(profile, body.api_key.get_secret_value() or None)
        except ValueError as error:
            raise HTTPException(400, str(error))
        except OSError:
            raise HTTPException(500, "Could not write the profile file. Check disk permissions.")

    @app.delete("/api/profiles/{name}", dependencies=[Depends(auth)])
    async def delete_profile(name: str):
        profiles.delete(name)
        return {"ok": True}

    async def worker(run, demo, state):
        async with busy:
            if demo:
                from .demo import DemoProvider, demo_connect

                await execute(run, config, provider=DemoProvider(), connector=demo_connect)
            else:
                await execute(run, config, state=state)

    def start(settings, demo=False, state=None):
        if busy.locked() or any(r.status not in TERMINAL for r in runs.values()):
            raise HTTPException(409, "Wait for or stop the current run")
        if len(runs) >= 50:
            raise HTTPException(429, "50-run session limit reached. Restart Astra; files remain on disk.")
        run = Run(settings, output)
        runs[run.id] = run
        if demo:
            run.emit("demo", message="DEMO: scripted model and simulated Blender. Images are illustrations.")
        run.task = asyncio.create_task(worker(run, demo, state))
        return {"id": run.id}

    def load_state(run_id):
        # run_id is pattern-checked to 32 hex characters, so this cannot escape
        # the output directory.
        path = output / run_id / "state.json"
        if not path.is_file():
            raise HTTPException(404, "That run has no saved state to continue from")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HTTPException(422, "That run's saved state is unreadable")

    @app.post("/api/runs", dependencies=[Depends(auth)])
    async def start_run(settings: RunConfig):
        # A remembered key is resolved here, never sent to or from the browser.
        if not settings.api_key.get_secret_value() and settings.profile:
            remembered = profiles.load_secret(settings.profile)
            if remembered:
                settings = settings.model_copy(update={"api_key": SecretStr(remembered)})
        return start(settings, state=load_state(settings.resume_from) if settings.resume_from else None)

    @app.post("/api/demo", dependencies=[Depends(auth)])
    async def demo():
        return start(
            RunConfig(prompt="Demo: sculptural studio composition", model="demo/scripted", auto_approve=True),
            demo=True,
        )

    @app.get("/api/runs/resumable", dependencies=[Depends(auth)])
    async def resumable():
        """Runs on disk that stopped before finishing, newest first.

        Read from the filesystem rather than memory so a run survives an Astra
        restart: the scene it built is still in Blender either way.
        """
        found = []
        for directory in sorted(output.iterdir()) if output.is_dir() else []:
            saved, manifest = directory / "state.json", directory / "manifest.json"
            if not saved.is_file():
                continue
            data, info = _read_json(saved), _read_json(manifest)
            if data is None or info is None:
                continue
            status, model = info.get("status"), info.get("model")
            # A demo run has no real scene behind it, so continuing one against
            # a live Blender would replay a simulated conversation.
            if status == "completed" or model == "demo/scripted":
                continue
            found.append(
                {
                    "id": directory.name,
                    "stage": data.get("stage"),
                    "steps": data.get("steps", 0),
                    "prompt": (data.get("prompt") or "")[:140],
                    "status": status,
                    "updated": saved.stat().st_mtime,
                }
            )
        found.sort(key=lambda entry: entry["updated"], reverse=True)
        return {"runs": found[:20]}

    @app.get("/api/runs/{run_id}", dependencies=[Depends(auth)])
    async def state(run_id: str, after: int = Query(default=0, ge=0)):
        return get_run(run_id).snapshot(after)

    class Decision(BaseModel):
        approved: bool

    @app.post("/api/runs/{run_id}/approvals/{approval_id}", dependencies=[Depends(auth)])
    async def approve(run_id: str, approval_id: str, decision: Decision):
        run = get_run(run_id)
        future = run.approvals.get(approval_id)
        if future is None or future.done():
            raise HTTPException(409, "Approval is no longer pending")
        future.set_result(decision.approved)
        run.emit("decision", approval_id=approval_id, approved=decision.approved)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(auth)])
    async def cancel(run_id: str):
        run = get_run(run_id)
        if run.task and not run.task.done():
            run.task.cancel()
            if run.status == "queued":
                run.status = "cancelled"
                run.emit("cancelled", message="Cancelled before connecting")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/files", dependencies=[Depends(auth)])
    async def files(run_id: str):
        run = get_run(run_id)
        return {"files": sorted(p.name for p in run.directory.iterdir() if p.is_file())}

    @app.get("/api/runs/{run_id}/files/{filename}", dependencies=[Depends(auth)])
    async def artifact(run_id: str, filename: str):
        run = get_run(run_id)
        path = (run.directory / filename).resolve()
        if path.parent != run.directory or not path.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(path, filename=path.name)

    static = Path(__file__).parent / "static"
    app.mount("/", StaticFiles(directory=static, html=True), name="studio")
    return app
