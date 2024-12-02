import asyncio
import json
import logging
from datetime import datetime, timezone
import os
from typing import Dict, Optional
import aiohttp
from aiomqtt import Client, MqttError  # Changed from asyncio_mqtt to aiomqtt
import signal
import numpy as np
import av
import io
import threading
from queue import Queue
import pathlib

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AudioStreamReader:
    def __init__(self, chunk_size=1024*8):
        self.chunk_size = chunk_size
        self.buffer = Queue()
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    async def read_stream(self, url: str) -> bool:
        """Read audio stream and check for silence"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return False

                    chunk = await response.content.read(self.chunk_size)
                    if not chunk:
                        return False

                    container = av.open(io.BytesIO(chunk))
                    stream = container.streams.audio[0]
                    
                    frame_count = 0
                    max_level = 0.0
                    
                    for frame in container.decode(stream):
                        frame_count += 1
                        if frame_count > 10:  # Check just a few frames
                            break
                            
                        samples = frame.to_ndarray().flatten()
                        level = np.abs(samples).max()
                        max_level = max(max_level, level)

                    return max_level > 0.001  # Adjust threshold as needed

        except Exception as e:
            logger.error(f"Error reading audio stream: {e}")
            return False

class StreamMonitor:
    def __init__(self):
        # MQTT Configuration
        self.mqtt_host = os.getenv('MQTT_HOST', 'localhost')
        self.mqtt_port = int(os.getenv('MQTT_PORT', '1883'))
        self.mqtt_user = os.getenv('MQTT_USER')
        self.mqtt_password = os.getenv('MQTT_PASSWORD')
        
        # Load stream configuration
        self.config_path = pathlib.Path('/app/config/streams.json')
        self.streams = self.load_stream_config()
        
        self.running = True
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

    def load_stream_config(self) -> Dict:
        """Load stream configuration from file"""
        try:
            with open(self.config_path) as f:
                config = json.load(f)
                
            streams = {}
            for stream_id, stream_config in config.items():
                streams[stream_id] = {
                    "url": stream_config["url"],
                    "name": stream_config["name"],
                    "online": False,
                    "silent": False,
                    "last_check": None,
                    "silence_start": None,
                    "online_start": None,
                    "offline_start": None,
                    "audio_reader": AudioStreamReader()
                }
            return streams
                
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise

    def signal_handler(self, signum, frame):
        logger.info("Shutdown signal received")
        self.running = False
        for stream in self.streams.values():
            if 'audio_reader' in stream:
                stream['audio_reader'].stop()

    async def publish_discovery(self, client: Client):
        """Publish Home Assistant MQTT discovery configs"""
        base_topic = "homeassistant"
        
        for stream_id, stream in self.streams.items():
            # Status sensor discovery
            status_config = {
                "name": f"{stream['name']} Status",
                "unique_id": f"azuracast_{stream_id}_status",
                "state_topic": f"azuracast/{stream_id}/status/state",
                "json_attributes_topic": f"azuracast/{stream_id}/status/attributes",
                "device_class": "connectivity",
                "device": {
                    "identifiers": [f"azuracast_{stream_id}"],
                    "name": f"AzuraCast {stream['name']}",
                    "model": "Stream Monitor",
                    "manufacturer": "AzuraCast"
                }
            }
            await client.publish(
                f"{base_topic}/binary_sensor/azuracast_{stream_id}_status/config",
                payload=json.dumps(status_config),
                qos=1,
                retain=True
            )
            
            # Silence sensor discovery
            silence_config = {
                "name": f"{stream['name']} Silence",
                "unique_id": f"azuracast_{stream_id}_silence",
                "state_topic": f"azuracast/{stream_id}/silence/state",
                "json_attributes_topic": f"azuracast/{stream_id}/silence/attributes",
                "device_class": "sound",
                "device": {
                    "identifiers": [f"azuracast_{stream_id}"],
                    "name": f"AzuraCast {stream['name']}",
                    "model": "Stream Monitor",
                    "manufacturer": "AzuraCast"
                }
            }
            await client.publish(
                f"{base_topic}/binary_sensor/azuracast_{stream_id}_silence/config",
                payload=json.dumps(silence_config),
                qos=1,
                retain=True
            )

    async def update_stream_state(self, client: Client, stream_id: str, online: bool, silent: Optional[bool] = None):
        """Update stream state and publish to MQTT"""
        stream = self.streams[stream_id]
        now = datetime.now(timezone.utc)
        
        # Handle online/offline state
        if online != stream['online']:
            stream['online'] = online
            if online:
                stream['online_start'] = now
                stream['offline_start'] = None
            else:
                stream['offline_start'] = now
                stream['online_start'] = None
                stream['silent'] = False  # Reset silence state when offline
                
            # Publish status state
            await client.publish(
                f"azuracast/{stream_id}/status/state",
                payload="ON" if online else "OFF",
                qos=1,
                retain=True
            )
            
            # Publish status attributes
            attributes = {
                "online_since": stream['online_start'].isoformat() if stream['online_start'] else None,
                "offline_since": stream['offline_start'].isoformat() if stream['offline_start'] else None
            }
            await client.publish(
                f"azuracast/{stream_id}/status/attributes",
                payload=json.dumps(attributes),
                qos=1,
                retain=True
            )
        
        # Handle silence state if stream is online
        if online and silent is not None and silent != stream['silent']:
            stream['silent'] = silent
            if silent:
                stream['silence_start'] = now
            
            # Publish silence state
            await client.publish(
                f"azuracast/{stream_id}/silence/state",
                payload="ON" if silent else "OFF",
                qos=1,
                retain=True
            )
            
            # Publish silence attributes
            attributes = {
                "silence_since": stream['silence_start'].isoformat() if stream['silence_start'] else None
            }
            await client.publish(
                f"azuracast/{stream_id}/silence/attributes",
                payload=json.dumps(attributes),
                qos=1,
                retain=True
            )

    async def check_stream(self, client: Client, stream_id: str):
        """Check a single stream's status and silence"""
        stream = self.streams[stream_id]
        
        try:
            # Check if stream has audio
            has_audio = await stream['audio_reader'].read_stream(stream['url'])
            if has_audio:
                await self.update_stream_state(client, stream_id, True, False)
            else:
                # If no audio detected, check if stream is actually available
                async with aiohttp.ClientSession() as session:
                    async with session.get(stream['url']) as response:
                        if response.status == 200:
                            # Stream available but silent
                            await self.update_stream_state(client, stream_id, True, True)
                        else:
                            # Stream not available
                            await self.update_stream_state(client, stream_id, False)
        except Exception as e:
            logger.error(f"Error checking stream {stream_id}: {e}")
            await self.update_stream_state(client, stream_id, False)

    async def monitor_streams(self):
        """Main monitoring loop"""
        try:
            async with Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.mqtt_user,
                password=self.mqtt_password
            ) as client:
                # Publish discovery configs
                await self.publish_discovery(client)
                
                # Main monitoring loop
                while self.running:
                    for stream_id in self.streams:
                        await self.check_stream(client, stream_id)
                    await asyncio.sleep(15)  # Check every 15 seconds
                
        except MqttError as error:
            logger.error(f'MQTT Error: {error}')

    async def run(self):
        """Run the monitor with error handling and reconnection"""
        while self.running:
            try:
                await self.monitor_streams()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(5)  # Wait before reconnecting

async def main():
    monitor = StreamMonitor()
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())
