import asyncio
import json
import logging
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

from .mpris import MPRISMonitor

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("dbus_music_overlay")

app = FastAPI(title="D-Bus Music OBS Overlay")

# Store active websocket connections
active_connections: list[WebSocket] = []
mpris_monitor = None

def on_mpris_update(state: dict):
    """Callback fired when MPRIS metadata changes. Broadcasts to all websockets."""
    message = json.dumps(state)
    logger.info(f"Broadcasting update: {message}")
    for connection in active_connections:
        asyncio.create_task(connection.send_text(message))

@app.on_event("startup")
async def startup_event():
    """Start the DBus monitor background task when the server starts."""
    global mpris_monitor
    mpris_monitor = MPRISMonitor(on_update=on_mpris_update)
    asyncio.create_task(mpris_monitor.start())

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for the frontend to receive real-time updates."""
    await websocket.accept()
    active_connections.append(websocket)
    
    # Send current state immediately on connect
    if mpris_monitor:
        await websocket.send_text(json.dumps(mpris_monitor.state))
        
    try:
        while True:
            # We don't expect messages from the client, just keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.remove(websocket)

@app.get("/")
async def get_index():
    """Serve the main HTML overlay template."""
    templates_dir = Path(__file__).parent / "templates"
    index_file = templates_dir / "index.html"
    return HTMLResponse(content=index_file.read_text(), status_code=200)

def start_server():
    """Entry point for the console script."""
    uvicorn.run("dbus_music_overlay.main:app", host="127.0.0.1", port=8000, log_level="info")

if __name__ == "__main__":
    start_server()
