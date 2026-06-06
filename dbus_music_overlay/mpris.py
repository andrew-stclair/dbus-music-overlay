import asyncio
import logging
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
        self.properties_interface = None
        
        # Store current state
        self.state = {
            "status": "Stopped",
            "title": "",
            "artist": "",
            "artUrl": ""
        }

    async def start(self):
        """Connects to the session bus and starts monitoring."""
        self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        
        try:
            # Introspect DBus to get a list of services
            dbus_introspection = await self.bus.introspect('org.freedesktop.DBus', '/org/freedesktop/DBus')
            dbus_obj = self.bus.get_proxy_object('org.freedesktop.DBus', '/org/freedesktop/DBus', dbus_introspection)
            dbus_iface = dbus_obj.get_interface('org.freedesktop.DBus')
            
            names = await dbus_iface.call_list_names()
            mpris_names = [name for name in names if name.startswith('org.mpris.MediaPlayer2.')]
            
            if mpris_names:
                await self.attach_to_player(mpris_names[0])

            # Listen for new media players starting or stopping
            def on_name_owner_changed(name: str, old_owner: str, new_owner: str):
                if name.startswith('org.mpris.MediaPlayer2.'):
                    if new_owner and not self.current_player:
                        asyncio.create_task(self.attach_to_player(name))
                    elif not new_owner and name == self.current_player:
                        logger.info(f"Player {name} disconnected.")
                        self.current_player = None
                        self.update_state({"status": "Stopped", "title": "", "artist": "", "artUrl": ""})
                        asyncio.create_task(self.check_other_players(dbus_iface))

            dbus_iface.on_name_owner_changed(on_name_owner_changed)
            logger.info("Connected to D-Bus and listening for MPRIS players.")
        except Exception as e:
            logger.error(f"Failed to setup DBus monitoring: {e}")

    async def check_other_players(self, dbus_iface):
        """Finds another active player if the current one stops."""
        names = await dbus_iface.call_list_names()
        mpris_names = [name for name in names if name.startswith('org.mpris.MediaPlayer2.')]
        if mpris_names:
            await self.attach_to_player(mpris_names[0])

    async def attach_to_player(self, player_name: str):
        """Attaches to a specific MPRIS player and reads its metadata."""
        try:
            logger.info(f"Attaching to player: {player_name}")
            introspection = await self.bus.introspect(player_name, '/org/mpris/MediaPlayer2')
            obj = self.bus.get_proxy_object(player_name, '/org/mpris/MediaPlayer2', introspection)
            
            self.properties_interface = obj.get_interface('org.freedesktop.DBus.Properties')
            player_interface = obj.get_interface('org.mpris.MediaPlayer2.Player')
            
            self.current_player = player_name
            
            # Initial fetch
            try:
                playback_status = await self.properties_interface.call_get('org.mpris.MediaPlayer2.Player', 'PlaybackStatus')
                metadata = await self.properties_interface.call_get('org.mpris.MediaPlayer2.Player', 'Metadata')
                self.handle_metadata_change(playback_status.value, metadata.value)
            except DBusError as e:
                logger.warning(f"Could not fetch initial properties from {player_name}: {e}")

            # Listen for changes
            def on_properties_changed(interface_name: str, changed_properties: dict, invalidated_properties: list):
                if interface_name == 'org.mpris.MediaPlayer2.Player':
                    status = changed_properties.get('PlaybackStatus')
                    metadata = changed_properties.get('Metadata')
                    
                    status_val = status.value if status else None
                    metadata_val = metadata.value if metadata else None
                    
                    self.handle_metadata_change(status_val, metadata_val)

            self.properties_interface.on_properties_changed(on_properties_changed)
            logger.info(f"Successfully attached and listening to {player_name}")

        except Exception as e:
            logger.error(f"Failed to attach to {player_name}: {e}")

    def handle_metadata_change(self, status: str | None, metadata: dict | None):
        """Parses raw DBus variant metadata into our simple state dictionary."""
        updates = {}
        if status is not None:
            updates["status"] = status
            
        if metadata is not None:
            # Metadata keys: xesam:title, xesam:artist, mpris:artUrl
            title = metadata.get('xesam:title')
            if title:
                updates["title"] = title.value
            else:
                updates["title"] = ""
                
            artist = metadata.get('xesam:artist')
            if artist:
                # Artist is usually a list of strings
                artist_val = artist.value
                if isinstance(artist_val, list):
                    updates["artist"] = ", ".join(artist_val)
                else:
                    updates["artist"] = str(artist_val)
            else:
                updates["artist"] = ""
                
            art_url = metadata.get('mpris:artUrl')
            if art_url:
                updates["artUrl"] = art_url.value
            else:
                updates["artUrl"] = ""

        if updates:
            self.update_state(updates)

    def update_state(self, updates: dict[str, Any]):
        """Updates internal state and calls the callback."""
        changed = False
        for k, v in updates.items():
            if self.state.get(k) != v:
                self.state[k] = v
                changed = True
                
        if changed:
            self.on_update(self.state)
