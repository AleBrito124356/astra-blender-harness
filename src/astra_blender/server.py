import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .bridge import connect, discover
from .config import MCPConfig, RunConfig
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

    async def worker(run, demo):
        async with busy:
            if demo:
                from .demo import DemoProvider, demo_connect

                await execute(run, config, provider=DemoProvider(), connector=demo_connect)
            else:
                await execute(run, config)

    def start(settings, demo=False):
        if busy.locked() or any(r.status not in TERMINAL for r in runs.values()):
            raise HTTPException(409, "Wait for or stop the current run")
        if len(runs) >= 50:
            raise HTTPException(429, "50-run session limit reached. Restart Astra; files remain on disk.")
        run = Run(settings, output)
        runs[run.id] = run
        if demo:
            run.emit("demo", message="DEMO: scripted model and simulated Blender. Images are illustrations.")
        run.task = asyncio.create_task(worker(run, demo))
        return {"id": run.id}

    @app.post("/api/runs", dependencies=[Depends(auth)])
    async def start_run(settings: RunConfig):
        return start(settings)

    @app.post("/api/demo", dependencies=[Depends(auth)])
    async def demo():
        return start(
            RunConfig(prompt="Demo: sculptural studio composition", model="demo/scripted", auto_approve=True),
            demo=True,
        )

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
