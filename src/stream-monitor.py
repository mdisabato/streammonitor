#!/usr/bin/env python3

"""
AzuraCast Stream Monitor
-----------------------
A Python script to monitor AzuraCast radio streams and report their status to Home Assistant via MQTT.

Features:
- Monitors multiple radio streams for availability and audio presence
- Reports stream status and silence detection
- Integrates with Home Assistant through MQTT auto-discovery
- Groups all stations under a single device in Home Assistant
- Provides detailed status attributes including timestamps

Configuration Requirements:
-------------------------
1. Environment Variables:
   MQTT_HOST: MQTT broker hostname (default: localhost)
   MQTT_PORT: MQTT broker port (default: 1883)
   MQTT_USER: MQTT username
   MQTT_PASSWORD: MQTT password

2. Configuration File (config/streams.json):
   {
     "station_id": {
       "url": "http://stream.url:port/mount.mp3",
       "name": "Display Name"
     },
     ...
   }
   Example:
   {
     "christmas_music": {
       "url": "http://server:8220/radio.mp3",
       "name": "Christmas Music"
     }
   }

Home Assistant Integration:
-------------------------
The script creates:
1. A single device called "Radio Stations"
2. For each station:
   - Binary sensor for online/offline status
   - Binary sensor for silence detection
   - Attributes including timestamps for status changes

Each sensor includes:
- Online/offline status
- Silence detection
- Last update time
- Duration calculations
- Auto-discovery configuration

Troubleshooting:
---------------
1. Check MQTT connection:
   - Verify MQTT broker is accessible
   - Check credentials
   - Confirm MQTT topics in broker

2. Stream Monitoring:
   - Verify stream URLs are accessible
   - Check audio detection threshold
   - Monitor logs for connection issues

3. Home Assistant:
   - Verify MQTT integration is configured
   - Check entity creation
   - Monitor states and attributes

Dependencies:
------------
pip install aiomqtt aiohttp numpy av

Author: Created through collaboration
License: MIT
"""

# Standard library imports
import signal
import asyncio
import json
import logging
from datetime import datetime, timezone
import os
from typing import Dict, Optional
import pathlib
import threading
from queue import Queue
import time

# Add yaml import
import yaml  # Add to dependencies in documentation

# Third-party imports
import aiohttp
from aiomqtt import Client, MqttError
import numpy as np
import av
import io

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Exception handling for graceful shutdown
class ProgramKilled(Exception):
    pass

def signal_handler(signum, frame):
    raise ProgramKilled

#
# Audio Stream Processing
#
# Add at top with other imports
import time

class AudioStreamReader:
    def __init__(self, chunk_size=8192, silence_threshold=-50.0, silence_duration=15, 
                 debounce_time=5):
        """
        Initialize the audio stream reader with configurable parameters
        """
        self.chunk_size = chunk_size
        self.silence_threshold_db = silence_threshold
        self.silence_duration = silence_duration
        self.debounce_time = debounce_time
        self.buffer = Queue()
        self._stop = threading.Event()
        
        # Historical data for dynamic adjustment
        self.level_history = []
        self.HISTORY_SIZE = 100
        self.silence_start_time = None
        self.last_state_change = None

    def amplitude_to_db(self, amplitude):
        """Convert raw amplitude to decibels"""
        return 20 * np.log10(amplitude + 1e-10)

    def calculate_rms(self, samples):
        """Calculate Root Mean Square of audio samples"""
        return np.sqrt(np.mean(np.square(samples)))

    def is_silent(self, level_db):
        """Determine if audio level indicates silence"""
        return level_db < self.silence_threshold_db

    async def read_stream(self, url: str) -> tuple[bool, float, dict]:
        """
        Read and analyze audio stream for silence detection
        
        Returns:
            tuple: (is_silent: bool, level_db: float, format_info: dict)
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.warning(f"HTTP Status {response.status} for {url}")
                        return True, None, None

                    chunk = await response.content.read(self.chunk_size)
                    if not chunk:
                        logger.warning(f"No data received from {url}")
                        return True, None, None

                    try:
                        container = av.open(io.BytesIO(chunk))
                        stream = container.streams.audio[0]
                        
                        format_info = {
                            'sample_rate': stream.sample_rate,
                            'channels': stream.channels,
                            'format': stream.format.name
                        }
                        
                        # Analyze multiple frames
                        frame_levels = []
                        current_time = time.time()
                        
                        for frame in container.decode(stream):
                            samples = frame.to_ndarray().flatten()
                            rms_level = self.calculate_rms(samples)
                            level_db = self.amplitude_to_db(rms_level)
                            frame_levels.append(level_db)
                            logger.debug(f"Frame level: {level_db:.2f} dB")
                            
                            if len(frame_levels) >= 10:  # Analyze about 10 frames
                                break
                        
                        if not frame_levels:
                            logger.warning("No audio frames decoded")
                            return True, None, format_info
                        
                        # Calculate average level
                        avg_level_db = np.mean(frame_levels)
                        logger.debug(f"Average level: {avg_level_db:.2f} dB")
                        
                        # Check silence threshold
                        is_current_frame_silent = self.is_silent(avg_level_db)
                        
                        # Handle silence duration and debouncing
                        if is_current_frame_silent:
                            if self.silence_start_time is None:
                                self.silence_start_time = current_time
                            
                            silence_duration = current_time - self.silence_start_time
                            if silence_duration >= self.silence_duration:
                                if (self.last_state_change is None or 
                                    current_time - self.last_state_change >= self.debounce_time):
                                    self.last_state_change = current_time
                                    logger.info(f"Silence detected: {avg_level_db:.2f} dB")
                                    return True, avg_level_db, format_info
                        else:
                            self.silence_start_time = None
                            if (self.last_state_change is None or 
                                current_time - self.last_state_change >= self.debounce_time):
                                self.last_state_change = current_time
                                logger.info(f"Audio detected: {avg_level_db:.2f} dB")
                                return False, avg_level_db, format_info

                        # Return previous state if within debounce period
                        return self.silence_start_time is not None, avg_level_db, format_info

                    except Exception as e:
                        logger.error(f"Error processing audio data: {str(e)}")
                        return True, None, None

        except Exception as e:
            logger.error(f"Error reading audio stream: {str(e)}")
            return True, None, None

    def stop(self):
        """Stop the audio reader"""
        self._stop.set()

#
# Main Stream Monitor Class
#
class StreamMonitor:
    def __init__(self):
        logger.info("Initializing Stream Monitor")
        self.mqtt_client = None
        self.running = True
        
        # Load configuration
        self.config_path = pathlib.Path('/app/config/config.yaml')
        logger.info("Loading configuration...")
        self.config = self.load_config()
        
        # Initialize from YAML config
        self.mqtt_host = self.config['mqtt']['mqtt_broker']
        self.mqtt_port = int(self.config['mqtt']['mqtt_port'])
        self.mqtt_user = self.config['mqtt']['mqtt_username']
        self.mqtt_password = self.config['mqtt']['mqtt_password']
        self.devicename = self.config['mqtt']['mqtt_topic_prefix']
        self.chunk_size = self.config['mqtt'].get('chunk_size', 8192)
        
        # Configure logging level from config
        log_level = self.config['mqtt'].get('log_level', 'INFO')
        logging.getLogger().setLevel(getattr(logging, log_level))
        
        logger.info(f"MQTT Configuration - Host: {self.mqtt_host}, Port: {self.mqtt_port}")
        
        # Load streams
        self.streams = self.initialize_streams()
        
        # Set up signal handlers
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        logger.info("Stream Monitor initialized successfully")

    def load_config(self) -> Dict:
        """Load configuration from YAML file"""
        try:
            logger.info(f"Reading configuration file from: {self.config_path}")
            if not self.config_path.exists():
                logger.error(f"Configuration file not found at {self.config_path}")
                raise FileNotFoundError(f"No config file at {self.config_path}")
                
            with open(self.config_path) as f:
                config = yaml.safe_load(f)
                logger.info("Successfully parsed configuration file")
            
            return config
                
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise

    def initialize_streams(self) -> Dict:
        """Initialize streams from YAML configuration"""
        streams = {}
        for station in self.config['stations']:
            stream_id = station['station_name']
            logger.info(f"Configuring stream: {stream_id} at {station['station_url']}")
            
            streams[stream_id] = {
                "url": station['station_url'],
                "name": stream_id.replace('_', ' ').title(),
                "silence_threshold": station.get('silence_threshold', -50.0),
                "silence_duration": station.get('silence_duration', 15),
                "debounce_time": station.get('debounce_time', 5),
                "online": False,
                "silent": False,
                "last_check": None,
                "silence_start": None,
                "online_start": None,
                "offline_start": None,
                "audio_reader": AudioStreamReader(
                    chunk_size=self.chunk_size,
                    silence_threshold=station.get('silence_threshold', -50.0),
                    silence_duration=station.get('silence_duration', 15),
                    debounce_time=station.get('debounce_time', 5)
                )
            }
            
            logger.info(f"Configured {streams[stream_id]['name']} with:")
            logger.info(f"  Silence threshold: {streams[stream_id]['silence_threshold']} dB")
            logger.info(f"  Silence duration: {streams[stream_id]['silence_duration']} seconds")
            logger.info(f"  Debounce time: {streams[stream_id]['debounce_time']} seconds")
        
        logger.info(f"Successfully configured {len(streams)} streams")
        return streams

    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info("Shutdown signal received")
        self.running = False
        for stream in self.streams.values():
            if 'audio_reader' in stream:
                stream['audio_reader'].stop()

    async def publish_discovery(self, client: Client):
        """Publish Home Assistant MQTT discovery configs"""
        logger.info("Publishing MQTT discovery configurations")
        base_topic = self.config['mqtt'].get('discovery_topic', 'homeassistant')
        device_id = self.config['mqtt'].get('device_unique_id', 'stations')
        
        device_config = {
            "identifiers": [device_id],
            "name": "Radio Stations",
            "model": "Stream Monitor",
            "manufacturer": "Dreamsong"
        }

        for stream_id, stream in self.streams.items():
            logger.info(f"Publishing discovery config for stream: {stream_id}")

            status_config = {
                "name": f"{stream['name']} Status",
                "state_topic": f"{self.devicename}/sensor/{stream_id}/state",
                "value_template": "{{value_json.status}}",
                "unique_id": f"{device_id}_{stream_id}_status",
                "object_id": f"{device_id}_{stream_id}_status",
                "availability_topic": f"{self.devicename}/sensor/{stream_id}/availability",
                "device_class": "connectivity",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_config,
                "icon": "mdi:radio"
            }

            await client.publish(
                f"{base_topic}/binary_sensor/{stream_id}/status/config",
                payload=json.dumps(status_config).encode(),
                qos=1,
                retain=True
            )

            silence_config = {
                "name": f"{stream['name']} Silence",
                "state_topic": f"{self.devicename}/sensor/{stream_id}/state",
                "value_template": "{{value_json.silence}}",
                "unique_id": f"{device_id}_{stream_id}_silence",
                "object_id": f"{device_id}_{stream_id}_silence",
                "availability_topic": f"{self.devicename}/sensor/{stream_id}/availability",
                "device_class": "problem",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_config,
                "icon": "mdi:volume-off"
            }

            await client.publish(
                f"{base_topic}/binary_sensor/{stream_id}/silence/config",
                payload=json.dumps(silence_config).encode(),
                qos=1,
                retain=True
            )

            await client.publish(
                f"{self.devicename}/sensor/{stream_id}/availability",
                payload="online",
                qos=1,
                retain=True
            )

      async def check_stream(self, client: Client, stream_id: str):
           """Check a single stream's status and silence"""
           stream = self.streams[stream_id]
           logger.info(f"Checking stream {stream_id} ({stream['name']}) at {stream['url']}")
           
           try:
               # Check stream status and audio levels
               is_silent, level_db, format_info = await stream['audio_reader'].read_stream(stream['url'])
               
               if level_db is not None:  # Stream is accessible
                   if is_silent:
                       logger.info(f"Stream {stream_id} is available but silent (Level: {level_db:.2f}dB)")
                       await self.update_sensor_state(client, stream_id, True, True)
                   else:
                       logger.info(f"Stream {stream_id} is available with audio detected (Level: {level_db:.2f}dB)")
                       await self.update_sensor_state(client, stream_id, True, False)
                   
                   # Include audio level in state payload
                   state_payload = {
                       'status': 'ON',
                       'silence': 'ON' if is_silent else 'OFF',
                       'level_db': round(level_db, 2),
                       'last_update': datetime.now(timezone.utc).isoformat()
                   }
                   
                   await client.publish(
                       f"{self.devicename}/sensor/{stream_id}/state",
                       payload=json.dumps(state_payload),
                       qos=1,
                       retain=True
                   )
               else:
                   logger.warning(f"Stream {stream_id} is not accessible")
                   await self.update_sensor_state(client, stream_id, False)
               
           except Exception as e:
               logger.error(f"Error checking stream {stream_id}: {e}")
               await self.update_sensor_state(client, stream_id, False)

    async def update_sensor_state(self, client: Client, stream_id: str, online: bool, silent: Optional[bool] = None):
        """Update sensor states and publish to MQTT"""
        stream = self.streams[stream_id]
        now = datetime.now(timezone.utc)
        
        # Update status state
        if online != stream['online']:
            stream['online'] = online
            if online:
                stream['online_start'] = now
                stream['offline_start'] = None
                logger.info(f"Stream {stream_id} is now online")
            else:
                stream['offline_start'] = now
                stream['online_start'] = None
                stream['silent'] = False
                logger.info(f"Stream {stream_id} is now offline")

        # Update silence state
        if online and silent is not None and silent != stream['silent']:
            stream['silent'] = silent
            if silent:
                stream['silence_start'] = now
                logger.info(f"Silence detected on stream {stream_id}")
            else:
                logger.info(f"Audio resumed on stream {stream_id}")

        # Build combined state payload
        state_payload = {
            'status': 'ON' if online else 'OFF',
            'silence': 'ON' if silent else 'OFF',
            'online_since': stream['online_start'].isoformat() if stream['online_start'] else None,
            'offline_since': stream['offline_start'].isoformat() if stream['offline_start'] else None,
            'silence_since': stream['silence_start'].isoformat() if stream['silence_start'] else None,
            'last_update': now.isoformat()
        }

        # Publish state update
        await client.publish(
            f"{self.devicename}/sensor/{stream_id}/state",
            payload=json.dumps(state_payload),
            qos=1,
            retain=True
        )

    async def monitor_streams(self):
        """Main monitoring loop"""
        try:
            logger.info(f"Attempting MQTT connection to {self.mqtt_host}:{self.mqtt_port}")
            async with Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.mqtt_user,
                password=self.mqtt_password
            ) as client:
                logger.info("MQTT connection established successfully")
                logger.info("Publishing discovery configs...")
                await self.publish_discovery(client)
                logger.info("Discovery configs published")
                
                while self.running:
                    # Update availability for each stream
                    for stream_id in self.streams:
                        await client.publish(
                            f"{self.devicename}/sensor/{stream_id}/availability",
                            payload="online",
                            qos=1,
                            retain=True
                        )
                    
                    logger.info("=== Starting stream check cycle ===")
                    for stream_id in self.streams:
                        await self.check_stream(client, stream_id)
                    logger.info("=== Stream check cycle complete ===")
                    await asyncio.sleep(15)
                
        except MqttError as error:
            logger.error(f'MQTT Error: {error}')

#
# Main Program Entry Point
#
async def main():
    """Main program entry point"""
    logger.info("=== Stream Monitor Starting ===")
    monitor = StreamMonitor()
    
    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        logger.info("Starting monitor process...")
        while True:
            try:
                await monitor.monitor_streams()
            except Exception as e:
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(5)  # Wait before retry
            
            if not monitor.running:
                break
                
    except ProgramKilled:
        logger.info("Program killed: running cleanup code")
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt: running cleanup code")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        # Cleanup
        logger.info("Shutting down...")
        monitor.running = False

if __name__ == "__main__":
    asyncio.run(main())
