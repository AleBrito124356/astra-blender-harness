import asyncio
import json
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
        if run_id not in runs:
            raise HTTPException(404, "Run not found in this process; previous files remain in runs/")
        return runs[run_id]

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
            try:
                data = json.loads(saved.read_text(encoding="utf-8"))
                status = json.loads(manifest.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError):
                continue
            if status == "completed":
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
