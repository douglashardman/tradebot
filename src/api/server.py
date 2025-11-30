"""FastAPI server for the trading dashboard."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.core.types import Signal
from src.execution.session import TradingSession
from src.execution.manager import ExecutionManager
from src.execution.orders import Trade, Position

logger = logging.getLogger(__name__)

# Get project root for static files
PROJECT_ROOT = Path(__file__).parent.parent.parent
STATIC_DIR = PROJECT_ROOT / "static"

# Global state
active_session: Optional[TradingSession] = None
execution_manager: Optional[ExecutionManager] = None
strategy_router: Optional[object] = None  # StrategyRouter instance
connected_clients: List[WebSocket] = []
signal_history: List[dict] = []


# === Pydantic Models ===

class SessionCreate(BaseModel):
    mode: str = "paper"
    symbol: str = "MES"
    daily_profit_target: float = 500.0
    daily_loss_limit: float = -300.0
    max_position_size: int = 2
    paper_starting_balance: float = 10000.0
    stop_loss_ticks: int = 5       # Scalping: tight stop
    take_profit_ticks: int = 4     # Scalping: quick target
    min_signal_strength: float = 0.6
    min_regime_confidence: float = 0.7


class SessionUpdate(BaseModel):
    daily_profit_target: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    max_position_size: Optional[int] = None


class SettingsUpdate(BaseModel):
    symbol: Optional[str] = None
    mode: Optional[str] = None
    daily_profit_target: Optional[float] = None
    daily_loss_limit: Optional[float] = None
    max_position_size: Optional[int] = None
    stop_loss_ticks: Optional[int] = None
    take_profit_ticks: Optional[int] = None


class StatusResponse(BaseModel):
    active: bool
    mode: Optional[str] = None
    symbol: Optional[str] = None
    daily_pnl: float = 0.0
    is_halted: bool = False
    halt_reason: Optional[str] = None
    open_positions: int = 0
    completed_trades: int = 0
    current_regime: Optional[str] = None
    regime_confidence: float = 0.0


# === Lifespan ===

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Order Flow Trading Dashboard")
    yield
    logger.info("Shutting down dashboard")
    # Cleanup: close any active session
    if active_session:
        await stop_session_internal()


# === FastAPI App ===

app = FastAPI(
    title="Order Flow Trading Dashboard",
    description="Real-time order flow trading system dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for local development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files if directory exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# === REST Endpoints ===

@app.get("/dashboard")
async def dashboard():
    """Serve the dashboard HTML."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    raise HTTPException(status_code=404, detail="Dashboard not found")

@app.get("/")
async def root():
    """Health check."""
    return {"status": "ok", "service": "Order Flow Trading Dashboard"}


@app.post("/api/session/start")
async def start_session(config: SessionCreate):
    """Start a new trading session."""
    global active_session, execution_manager

    if active_session:
        raise HTTPException(status_code=400, detail="Session already active")

    # Create session
    active_session = TradingSession(
        mode=config.mode,
        symbol=config.symbol,
        daily_profit_target=config.daily_profit_target,
        daily_loss_limit=config.daily_loss_limit,
        max_position_size=config.max_position_size,
        paper_starting_balance=config.paper_starting_balance,
        stop_loss_ticks=config.stop_loss_ticks,
        take_profit_ticks=config.take_profit_ticks,
        min_signal_strength=config.min_signal_strength,
        min_regime_confidence=config.min_regime_confidence,
    )
    active_session.started_at = datetime.now()

    # Create execution manager
    execution_manager = ExecutionManager(active_session)

    # Register callbacks
    execution_manager.on_trade(on_trade_complete)
    execution_manager.on_position(on_new_position)

    logger.info(f"Session started: {active_session.session_id} ({config.mode} mode)")

    await broadcast({
        "type": "session_started",
        "data": active_session.to_dict()
    })

    return {"status": "started", "session": active_session.to_dict()}


@app.post("/api/session/stop")
async def stop_session():
    """Stop the current trading session."""
    summary = await stop_session_internal()
    if summary is None:
        raise HTTPException(status_code=400, detail="No active session")
    return {"status": "stopped", "summary": summary}


async def stop_session_internal() -> Optional[dict]:
    """Internal session stop logic."""
    global active_session, execution_manager

    if not active_session:
        return None

    # Get final statistics
    stats = execution_manager.get_statistics() if execution_manager else {}
    state = execution_manager.get_state() if execution_manager else {}

    summary = {
        "session_id": active_session.session_id,
        "duration_minutes": (
            (datetime.now() - active_session.started_at).seconds // 60
            if active_session.started_at else 0
        ),
        "daily_pnl": state.get("daily_pnl", 0),
        "total_trades": stats.get("total_trades", 0),
        "win_rate": stats.get("win_rate", 0),
    }

    active_session.ended_at = datetime.now()

    await broadcast({
        "type": "session_stopped",
        "data": summary
    })

    active_session = None
    execution_manager = None

    logger.info(f"Session stopped: {summary}")
    return summary


@app.get("/api/session/status", response_model=StatusResponse)
async def get_status():
    """Get current session status."""
    if not active_session:
        return StatusResponse(active=False)

    state = execution_manager.get_state() if execution_manager else {}

    # Get regime from router if available
    current_regime = None
    regime_confidence = 0.0
    if strategy_router and hasattr(strategy_router, 'get_state'):
        router_state = strategy_router.get_state()
        current_regime = router_state.get("current_regime")
        regime_confidence = router_state.get("regime_confidence", 0.0)

    return StatusResponse(
        active=True,
        mode=active_session.mode,
        symbol=active_session.symbol,
        daily_pnl=state.get("daily_pnl", 0),
        is_halted=state.get("is_halted", False),
        halt_reason=state.get("halt_reason"),
        open_positions=state.get("open_positions", 0),
        completed_trades=state.get("completed_trades", 0),
        current_regime=current_regime,
        regime_confidence=regime_confidence,
    )


@app.patch("/api/session/limits")
async def update_limits(update: SessionUpdate):
    """Update session limits (live)."""
    if not active_session:
        raise HTTPException(status_code=400, detail="No active session")

    if update.daily_profit_target is not None:
        active_session.daily_profit_target = update.daily_profit_target
    if update.daily_loss_limit is not None:
        active_session.daily_loss_limit = update.daily_loss_limit
    if update.max_position_size is not None:
        active_session.max_position_size = update.max_position_size

    await broadcast({
        "type": "limits_updated",
        "data": active_session.to_dict()
    })

    return {"status": "updated", "session": active_session.to_dict()}


@app.post("/api/session/halt")
async def halt_session(reason: str = "Manual halt"):
    """Halt trading and close all open positions immediately."""
    if not execution_manager:
        raise HTTPException(status_code=400, detail="No active session")

    # Close all open positions first
    closed_trades = []
    if execution_manager.open_positions:
        # Get current price from last position or use entry price
        for pos in list(execution_manager.open_positions):
            current_price = pos.current_price or pos.entry_price
            trade = execution_manager._close_position(pos, current_price, "HALT")
            closed_trades.append(trade.to_dict())

    execution_manager._halt(reason)

    await broadcast({
        "type": "session_halted",
        "data": {"reason": reason, "closed_trades": closed_trades}
    })

    return {"status": "halted", "reason": reason, "closed_trades": len(closed_trades)}


@app.post("/api/session/resume")
async def resume_session():
    """Resume trading."""
    if not execution_manager:
        raise HTTPException(status_code=400, detail="No active session")

    execution_manager.resume()

    await broadcast({
        "type": "session_resumed",
        "data": {}
    })

    return {"status": "resumed"}


@app.post("/api/session/reset")
async def reset_session():
    """Reset paper trading session - clears P&L, trades, and positions."""
    if not execution_manager or not active_session:
        raise HTTPException(status_code=400, detail="No active session")

    if active_session.mode != "paper":
        raise HTTPException(status_code=400, detail="Reset only available in paper trading mode")

    # Close any open positions at entry price (no P&L impact)
    for pos in list(execution_manager.open_positions):
        execution_manager.open_positions.remove(pos)

    # Reset all stats
    execution_manager.daily_pnl = 0.0
    execution_manager.completed_trades = []
    execution_manager.pending_orders = []
    execution_manager.is_halted = False
    execution_manager.halt_reason = None
    execution_manager.paper_balance = active_session.paper_starting_balance

    # Reset session timestamps
    active_session.started_at = datetime.now()

    await broadcast({
        "type": "session_reset",
        "data": {"paper_balance": execution_manager.paper_balance}
    })

    return {"status": "reset", "paper_balance": execution_manager.paper_balance}


@app.get("/api/settings")
async def get_settings():
    """Get current settings."""
    if not active_session:
        return {
            "symbol": "MES",
            "mode": "paper",
            "daily_profit_target": 500.0,
            "daily_loss_limit": -300.0,
            "max_position_size": 1,
            "stop_loss_ticks": 5,
            "take_profit_ticks": 4,
        }
    return {
        "symbol": active_session.symbol,
        "mode": active_session.mode,
        "daily_profit_target": active_session.daily_profit_target,
        "daily_loss_limit": active_session.daily_loss_limit,
        "max_position_size": active_session.max_position_size,
        "stop_loss_ticks": active_session.stop_loss_ticks,
        "take_profit_ticks": active_session.take_profit_ticks,
    }


@app.post("/api/settings")
async def update_settings(settings: SettingsUpdate):
    """Update trading settings. Requires stopping and restarting session for some changes."""
    global active_session

    if not active_session:
        raise HTTPException(status_code=400, detail="No active session")

    # Update what we can
    if settings.symbol is not None and execution_manager:
        execution_manager.update_symbol(settings.symbol)
    if settings.daily_profit_target is not None:
        active_session.daily_profit_target = settings.daily_profit_target
    if settings.daily_loss_limit is not None:
        active_session.daily_loss_limit = settings.daily_loss_limit
    if settings.max_position_size is not None:
        active_session.max_position_size = settings.max_position_size
    if settings.stop_loss_ticks is not None:
        active_session.stop_loss_ticks = settings.stop_loss_ticks
    if settings.take_profit_ticks is not None:
        active_session.take_profit_ticks = settings.take_profit_ticks

    # Broadcast the update
    await broadcast({
        "type": "settings_updated",
        "data": {
            "symbol": active_session.symbol,
            "mode": active_session.mode,
            "daily_profit_target": active_session.daily_profit_target,
            "daily_loss_limit": active_session.daily_loss_limit,
            "max_position_size": active_session.max_position_size,
            "stop_loss_ticks": active_session.stop_loss_ticks,
            "take_profit_ticks": active_session.take_profit_ticks,
        }
    })

    return {"status": "updated", "settings": {
        "symbol": active_session.symbol,
        "mode": active_session.mode,
        "daily_profit_target": active_session.daily_profit_target,
        "daily_loss_limit": active_session.daily_loss_limit,
        "max_position_size": active_session.max_position_size,
        "stop_loss_ticks": active_session.stop_loss_ticks,
        "take_profit_ticks": active_session.take_profit_ticks,
    }}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Get recent completed trades."""
    if not execution_manager:
        return []
    return [t.to_dict() for t in execution_manager.completed_trades[-limit:]]


@app.get("/api/positions")
async def get_positions():
    """Get open positions."""
    if not execution_manager:
        return []
    return [p.to_dict() for p in execution_manager.open_positions]


@app.get("/api/signals")
async def get_signals(limit: int = 100):
    """Get recent signals."""
    return signal_history[-limit:]


@app.get("/api/statistics")
async def get_statistics():
    """Get trading statistics."""
    if not execution_manager:
        return {}
    return execution_manager.get_statistics()


# === WebSocket ===

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates."""
    await websocket.accept()
    connected_clients.append(websocket)
    logger.info(f"Client connected. Total clients: {len(connected_clients)}")

    try:
        while True:
            # Keep connection alive
            # Could also handle incoming messages here if needed
            await asyncio.sleep(0.1)

            # Periodically send state update
            if active_session and execution_manager:
                state_data = execution_manager.get_state()

                # Add regime data if router is available
                if strategy_router and hasattr(strategy_router, 'get_state'):
                    router_state = strategy_router.get_state()
                    state_data["current_regime"] = router_state.get("current_regime", "NO_TRADE")
                    state_data["regime_confidence"] = router_state.get("regime_confidence", 0)
                    state_data["regime_bias"] = router_state.get("bias")
                    state_data["regime_description"] = router_state.get("description", "")

                state = {
                    "type": "state_update",
                    "timestamp": datetime.now().isoformat(),
                    "data": state_data
                }
                await websocket.send_json(state)
                await asyncio.sleep(1)  # Update every second

    except WebSocketDisconnect:
        connected_clients.remove(websocket)
        logger.info(f"Client disconnected. Total clients: {len(connected_clients)}")


async def broadcast(message: dict):
    """Broadcast message to all connected clients."""
    disconnected = []
    for client in connected_clients:
        try:
            await client.send_json(message)
        except Exception:
            disconnected.append(client)

    # Remove disconnected clients
    for client in disconnected:
        connected_clients.remove(client)


# === Callbacks ===

def on_trade_complete(trade: Trade):
    """Called when a trade completes."""
    asyncio.create_task(broadcast({
        "type": "trade",
        "data": trade.to_dict()
    }))


def on_new_position(position: Position):
    """Called when a new position is opened."""
    asyncio.create_task(broadcast({
        "type": "position",
        "data": position.to_dict()
    }))


async def broadcast_signal(signal: Signal):
    """Broadcast a signal to all clients."""
    signal_data = {
        "timestamp": signal.timestamp.isoformat(),
        "pattern": signal.pattern.value,
        "direction": signal.direction,
        "strength": signal.strength,
        "price": signal.price,
        "approved": signal.approved,
        "rejection_reason": signal.rejection_reason,
        "regime": signal.regime,
    }

    # Store in history
    signal_history.append(signal_data)
    if len(signal_history) > 500:
        signal_history.pop(0)

    await broadcast({
        "type": "signal",
        "data": signal_data
    })


async def broadcast_trade(trade):
    """Broadcast a completed trade to all clients."""
    trade_data = {
        "side": trade.side,
        "size": trade.size,
        "symbol": trade.symbol,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "pnl_ticks": trade.pnl_ticks,
    }
    await broadcast({
        "type": "trade",
        "data": trade_data
    })


# === Entry Point ===

def run_server(host: str = "0.0.0.0", port: int = 8000):
    """Run the FastAPI server."""
    import uvicorn
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_server()
