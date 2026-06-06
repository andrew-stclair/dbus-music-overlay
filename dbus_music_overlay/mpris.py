import asyncio
import logging
import time
from typing import Callable, Any
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.errors import DBusError

logger = logging.getLogger("mpris")

class MPRISMonitor:
    def __init__(self, on_update: Callable[[dict[str, Any]], None]):
        self.on_update = on_update
        self.bus: MessageBus | None = None
        self.current_player: str | None = None
        self.players: dict[str, dict[str, Any]] = {}
        
        # Store current broadcasted state
        self.state = {
            "status": "Stopped",
            "title": "",
            "artist": "",
            "artUrl": ""
        }

    def is_ignored_player(self, player_name: str) -> bool:
        ignored_patterns = [
            "firefox.instance",
            "chromium.instance",
            "chrome.instance",
            "brave.instance",
            "vivaldi.instance",
            "edge.instance",
            "opera.instance"
        ]
        return any(pattern in player_name.lower() for pattern in ignored_patterns)

    async def start(self):
        """Connects to the session bus and starts monitoring all players."""
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        
        try:
            dbus_introspection = await self.bus.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus')
            dbus_obj = self.bus.get_proxy_object('org.freedesktop.DBus', '/org/freedesktop/DBus', dbus_introspection)
            dbus_iface = dbus_obj.get_interface('org.freedesktop.DBus')
            
            names = await dbus_iface.call_list_names()
            mpris_names = [name for name in names if name.startswith('org.mpris.MediaPlayer2.')]
            
            for name in mpris_names:
                if not self.is_ignored_player(name):
                    asyncio.create_task(self.attach_to_player(name))

            def on_name_owner_changed(name: str, old_owner: str, new_owner: str):
                if name.startswith('org.mpris.MediaPlayer2.'):
                    if self.is_ignored_player(name):
                        return
                    if new_owner and name not in self.players:
                        asyncio.create_task(self.attach_to_player(name))
                    elif not new_owner and name in self.players:
                        self.detach_from_player(name)

            dbus_iface.on_name_owner_changed(on_name_owner_changed)
            logger.info("Connected to D-Bus and listening for MPRIS players.")
        except Exception as e:
            logger.error(f"Failed to setup DBus monitoring: {e}")

    def detach_from_player(self, player_name: str):
        logger.info(f"Player {player_name} disconnected.")
        if player_name in self.players:
            del self.players[player_name]
        self.reevaluate_active_player()

    async def attach_to_player(self, player_name: str):
        """Attaches to an MPRIS player and listens for its specific events."""
        try:
            logger.info(f"Attaching to player: {player_name}")
            introspection = await self.bus.introspect(player_name, '/org/mpris/MediaPlayer2')
            obj = self.bus.get_proxy_object(player_name, '/org/mpris/MediaPlayer2', introspection)
            
            properties_interface = obj.get_interface('org.freedesktop.DBus.Properties')
            
            self.players[player_name] = {
                "status": "Stopped",
                "metadata": {},
                "last_active": time.time()
            }
            
            # Initial fetch
            try:
                playback_status = await properties_interface.call_get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
                metadata = await properties_interface.call_get('org.mpris.MediaPlayer2.Player', 'Metadata')
                self.players[player_name]["status"] = playback_status.value
                self.players[player_name]["metadata"] = metadata.value
                self.reevaluate_active_player()
            except DBusError as e:
                logger.warning(f"Could not fetch initial properties from {player_name}: {e}")

            # Listen for changes
            def on_properties_changed(interface_name: str, changed_properties: dict, invalidated_properties: list):
                if interface_name == 'org.mpris.MediaPlayer2.Player':
                    if player_name not in self.players:
                        return
                    
                    status = changed_properties.get('PlaybackStatus')
                    metadata = changed_properties.get('Metadata')
                    
                    updated = False
                    if status is not None:
                        new_status = status.value
                        if self.players[player_name]["status"] != new_status:
                            self.players[player_name]["status"] = new_status
                            updated = True
                            
                    if metadata is not None:
                        old_meta = self.players[player_name]["metadata"]
                        new_meta = metadata.value
                        
                        def get_val(m, key):
                            val = m.get(key)
                            return val.value if val else None
                            
                        for key in ['xesam:title', 'xesam:artist', 'mpris:artUrl']:
                            if get_val(old_meta, key) != get_val(new_meta, key):
                                updated = True
                                break
                                
                        self.players[player_name]["metadata"].update(new_meta)
                        
                    if updated:
                        self.players[player_name]["last_active"] = time.time()
                        self.reevaluate_active_player()

            properties_interface.on_properties_changed(on_properties_changed)
            logger.info(f"Successfully attached and listening to {player_name}")

        except Exception as e:
            logger.error(f"Failed to attach to {player_name}: {e}")

    def reevaluate_active_player(self):
        """
        Determines which player should currently be displayed based on state and recency.
        """
        def get_score(item):
            player_name, data = item
            # Base score is the unix timestamp (newer is higher)
            score = data["last_active"]
            
            metadata = data.get("metadata", {})
            
            def get_val(m, key):
                val = m.get(key)
                return val.value if val else None
                
            # Give a tiny 100ms boost if they provide rich metadata
            if get_val(metadata, 'mpris:artUrl'):
                score += 0.1
            if get_val(metadata, 'xesam:artist'):
                score += 0.1
                
            return score

        # 1. Look for all 'Playing' players, sort by smart score
        playing = [(p, data) for p, data in self.players.items() if data["status"] == "Playing"]
        if playing:
            playing.sort(key=get_score, reverse=True)
            self.current_player = playing[0][0]
            self.broadcast_player_state(self.current_player)
            return
            
        # 2. Look for all 'Paused' players, sort by smart score
        paused = [(p, data) for p, data in self.players.items() if data["status"] == "Paused"]
        if paused:
            paused.sort(key=get_score, reverse=True)
            self.current_player = paused[0][0]
            self.broadcast_player_state(self.current_player)
            return
            
        # 3. None are playing or paused
        self.current_player = None
        self.update_state({"status": "Stopped", "title": "", "artist": "", "artUrl": ""})

    def broadcast_player_state(self, player_name: str):
        import urllib.parse
        import base64
        import mimetypes
        from pathlib import Path
        
        data = self.players.get(player_name)
        if not data:
            return
            
        status = data["status"]
        metadata = data["metadata"]
        
        updates = {"status": status}
        
        title = metadata.get('xesam:title')
        updates["title"] = title.value if title else ""
            
        artist = metadata.get('xesam:artist')
        if artist:
            artist_val = artist.value
            if isinstance(artist_val, list):
                updates["artist"] = ", ".join(artist_val)
            else:
                updates["artist"] = str(artist_val)
        else:
            updates["artist"] = ""
            
        art_url = metadata.get('mpris:artUrl')
        url = art_url.value if art_url else ""
        if url.startswith("file://"):
            try:
                # Remove file:// and decode the URL encoded path
                file_path = urllib.parse.unquote(url[7:])
                p = Path(file_path)
                if p.is_file():
                    mime_type, _ = mimetypes.guess_type(p)
                    if not mime_type:
                        mime_type = "image/png"
                    
                    with open(p, "rb") as image_file:
                        encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
                    url = f"data:{mime_type};base64,{encoded_string}"
                else:
                    url = ""
            except Exception as e:
                logger.error(f"Failed to read local artwork {url}: {e}")
                url = ""
                
        updates["artUrl"] = url
        
        self.update_state(updates)

    def update_state(self, updates: dict[str, Any]):
        """Updates internal broadcast state and triggers the callback if changed."""
        changed = False
        for k, v in updates.items():
            if self.state.get(k) != v:
                self.state[k] = v
                changed = True
                
        if changed:
            self.on_update(self.state)
