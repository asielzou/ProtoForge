import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from protoforge.api.v1.router import router
from protoforge.core.engine import SimulationEngine
from protoforge.core.log_bus import LogBus
from protoforge.core.template import TemplateManager
from protoforge.db.session import Database
from protoforge.protocols.http.server import HttpSimulatorServer
from protoforge.protocols.modbus.server import ModbusTcpServer
from protoforge.protocols.modbus.rtu_server import ModbusRtuServer
from protoforge.protocols.mqtt.server import MqttBroker
from protoforge.protocols.opcua.server import OpcUaServer
from protoforge.protocols.gb28181.server import GB28181Server
from protoforge.protocols.bacnet.server import BACnetServer
from protoforge.protocols.s7.server import S7Server
from protoforge.protocols.mc.server import McServer
from protoforge.protocols.fins.server import FinsServer
from protoforge.protocols.ab.server import AbServer
from protoforge.protocols.opcda.server import OpcDaServer
from protoforge.protocols.fanuc.server import FanucServer
from protoforge.protocols.mtconnect.server import MtConnectServer
from protoforge.protocols.toledo.server import ToledoServer

logger = logging.getLogger(__name__)

_engine: SimulationEngine | None = None
_template_manager: TemplateManager | None = None
_database: Database | None = None
_log_bus: LogBus | None = None


def get_engine() -> SimulationEngine:
    global _engine
    if _engine is None:
        raise RuntimeError("Engine not initialized")
    return _engine


def get_template_manager() -> TemplateManager:
    global _template_manager
    if _template_manager is None:
        raise RuntimeError("Template manager not initialized")
    return _template_manager


def get_database() -> Database:
    global _database
    if _database is None:
        raise RuntimeError("Database not initialized")
    return _database


def get_log_bus() -> LogBus:
    global _log_bus
    if _log_bus is None:
        raise RuntimeError("Log bus not initialized")
    return _log_bus


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _engine, _template_manager, _database, _log_bus

    _log_bus = LogBus()

    _template_manager = TemplateManager()
    _template_manager.load_builtin_templates()

    _database = Database()
    await _database.connect()

    _engine = SimulationEngine()
    _engine.register_protocol(ModbusTcpServer())
    _engine.register_protocol(ModbusRtuServer())
    _engine.register_protocol(OpcUaServer())
    _engine.register_protocol(MqttBroker())
    _engine.register_protocol(HttpSimulatorServer())
    _engine.register_protocol(GB28181Server())
    _engine.register_protocol(BACnetServer())
    _engine.register_protocol(S7Server())
    _engine.register_protocol(McServer())
    _engine.register_protocol(FinsServer())
    _engine.register_protocol(AbServer())
    _engine.register_protocol(OpcDaServer())
    _engine.register_protocol(FanucServer())
    _engine.register_protocol(MtConnectServer())
    _engine.register_protocol(ToledoServer())
    await _engine.start()

    try:
        saved_devices = await _database.load_all_devices()
        for dev in saved_devices:
            try:
                await _engine.create_device(dev)
            except Exception:
                pass
        logger.info("Restored %d devices from database", len(saved_devices))
    except Exception as e:
        logger.warning("Failed to restore devices: %s", e)

    try:
        saved_scenarios = await _database.load_all_scenarios()
        for sc in saved_scenarios:
            _engine.create_scenario(sc)
        logger.info("Restored %d scenarios from database", len(saved_scenarios))
    except Exception as e:
        logger.warning("Failed to restore scenarios: %s", e)

    try:
        saved_templates = await _database.load_all_templates()
        for tmpl in saved_templates:
            _template_manager.add_template(tmpl)
        logger.info("Restored %d templates from database", len(saved_templates))
    except Exception as e:
        logger.warning("Failed to restore templates: %s", e)

    try:
        from protoforge.api.v1.router import _get_test_runner
        runner = _get_test_runner()
        runner.set_database(_database)
        await runner.restore_from_db()
    except Exception as e:
        logger.warning("Failed to restore test data: %s", e)

    try:
        from protoforge.core.auth import user_manager
        user_manager.set_database(_database)
        await user_manager.restore_from_db()
    except Exception as e:
        logger.warning("Failed to restore users: %s", e)

    try:
        from protoforge.core.webhook import webhook_manager
        await webhook_manager.start()
    except Exception as e:
        logger.warning("Failed to start webhook manager: %s", e)

    import os
    if os.environ.get("PROTOFORGE_DEMO_MODE"):
        try:
            from protoforge.core.demo import seed_demo_data
            await seed_demo_data(_engine, _template_manager)
        except Exception as e:
            logger.warning("Failed to seed demo data: %s", e)

    logger.info("ProtoForge started")

    yield

    try:
        from protoforge.core.webhook import webhook_manager
        await webhook_manager.stop()
    except Exception:
        pass
    await _engine.stop()
    await _database.close()
    logger.info("ProtoForge stopped")


def create_app() -> FastAPI:
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import FileResponse, JSONResponse
    from pathlib import Path

    app = FastAPI(
        title="ProtoForge",
        description="物联网协议仿真与测试平台 API",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from protoforge.api.v1.auth import auth_middleware
    app.middleware("http")(auth_middleware)

    app.include_router(router)

    # ========== 保留的 API 端点(改成 /api/info,不再占用 /) ==========
    @app.get("/api/info")
    async def info():
        return {
            "name": "ProtoForge",
            "version": "0.1.0",
            "description": "物联网协议仿真与测试平台",
        }

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ========== 前端静态资源 ==========
    STATIC_DIR = Path("/app/static")

    if STATIC_DIR.exists():
        # 挂载 Vite 打包出的 assets 目录(js/css)
        assets_dir = STATIC_DIR / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

        # SPA 兜底路由:未匹配到的路径都返回 index.html,由 Vue Router 接管
        # 注意:这个必须放在所有路由注册之后!
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # 不拦截 API 路径(让它们走正常的 404)
            if full_path.startswith(("api/", "docs", "redoc", "openapi.json", "health")):
                return JSONResponse({"detail": "Not Found"}, status_code=404)

            # 优先返回真实存在的静态文件(favicon、logo 等根目录文件)
            candidate = STATIC_DIR / full_path
            if candidate.is_file():
                return FileResponse(candidate)

            # 其他一律回 index.html(支持 Vue Router history 模式刷新)
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                return FileResponse(index_file)

            return JSONResponse({"error": "frontend not built"}, status_code=404)

    return app


app = create_app()
