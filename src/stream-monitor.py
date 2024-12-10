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
- Configurable silence thresholds per stream
- Dynamic audio level monitoring
- Debounce protection against false positives

Configuration:
-------------
YAML configuration file (config/config.yaml):

mqtt:
  mqtt_broker: broker_address
  mqtt_port: 1883
  mqtt_username: user
  mqtt_password: pass
  mqtt_topic_prefix: radio-stations
  log_level: INFO  # INFO or DEBUG
  chunk_size: 8192  # Optional, bytes
  device_unique_id: stations
  discovery_topic: homeassistant

stations:
  - station_name: station_one
    station_url: http://stream.url:port/mount.mp3
    silence_threshold: -50.0  # in dB
    silence_duration: 15  # in seconds
    debounce_time: 5  # in seconds
  ...

Example:
  stations:
    - station_name: christmas_music
      station_url: http://server:8220/radio.mp3
      silence_threshold: -45.0
      silence_duration: 20
      debounce_time: 5

Home Assistant Integration:
-------------------------
The script creates:
1. A single device called "Radio Stations"
2. For each station:
   - Binary sensor for online/offline status
   - Binary sensor for silence detection
   - Attributes including:
     * Audio level in dB
     * Online/offline timestamps
     * Silence detection timestamps
     * Last update time

Troubleshooting:
---------------
1. Check MQTT connection:
   - Verify MQTT broker is accessible
   - Check credentials in YAML config
   - Confirm MQTT topics in broker

2. Stream Monitoring:
   - Verify stream URLs are accessible
   - Check silence_threshold in config
   - Monitor logs for connection issues
   - Verify audio levels in attributes

3. Home Assistant:
   - Verify MQTT integration is configured
   - Check entity creation
   - Monitor states and attributes

Dependencies:
------------
pip install pyyaml aiomqtt aiohttp numpy av

Author: Created through collaboration
License: MIT
"""

#
# Imports and Configuration
#
import signal
import asyncio
import logging
from datetime import datetime, timezone
import os
from typing import Dict, Optional, Tuple
import pathlib
import threading
from queue import Queue
import time
import json
import io

# Third-party imports
import yaml
import aiohttp
from aiomqtt import Client, MqttError
import numpy as np
import av

# Configure base logging - will be updated from config file
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

#
# Exception Handling
#
class ProgramKilled(Exception):
    pass

def signal_handler(signum, frame):
    raise ProgramKilled

#
# Audio Processing
#
class AudioStreamReader:
    def __init__(self, chunk_size: int = 8192, silence_threshold: float = -50.0, 
                 silence_duration: int = 15, debounce_time: int = 5):
        """
        Initialize the audio stream reader with configurable parameters
        
        Args:
            chunk_size: Size of audio chunks to read
            silence_threshold: dB level below which audio is considered silent
            silence_duration: Seconds of silence before triggering
            debounce_time: Seconds to wait before state changes
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

    def amplitude_to_db(self, amplitude: float) -> float:
        """Convert raw amplitude to decibels"""
        return 20 * np.log10(amplitude + 1e-10)

    def calculate_rms(self, samples: np.ndarray) -> float:
        """Calculate Root Mean Square of audio samples"""
        return np.sqrt(np.mean(np.square(samples)))

    def is_silent(self, level_db: float) -> bool:
        """Determine if audio level indicates silence"""
        return level_db < self.silence_threshold_db

    async def read_stream(self, url: str) -> Tuple[bool, Optional[float], Optional[dict]]:
        """
        Read and analyze audio stream for silence detection
        
        Returns:
            Tuple of (is_silent: bool, level_db: float, format_info: dict)
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        # Always log connection issues
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
                            # Debug level - frame-by-frame analysis
                            logger.debug(f"Frame level: {level_db:.2f} dB")
                            
                            if len(frame_levels) >= 10:  # Analyze about 10 frames
                                break
                        
                        if not frame_levels:
                            logger.warning("No audio frames decoded")
                            return True, None, format_info
                        
                        # Calculate average level
                        avg_level_db = np.mean(frame_levels)
                        # Debug level - average calculations
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
                                    # Always log silence detection
                                    logger.info(f"Silence detected: {avg_level_db:.2f} dB")
                                    return True, avg_level_db, format_info
                        else:
                            self.silence_start_time = None
                            if (self.last_state_change is None or 
                                current_time - self.last_state_change >= self.debounce_time):
                                self.last_state_change = current_time
                                # Always log audio detection
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
        """Initialize the Stream Monitor with configuration from YAML"""
        # Always log startup
        logger.info("Initializing Stream Monitor")
        self.mqtt_client = None
        self.running = True
        
        # Load configuration
        self.config_path = pathlib.Path('/app/config/config.yaml')
        logger.info("Loading configuration...")
        self.config = self.load_config()
        
        # Initialize from YAML config with environment variable override
        self.mqtt_host = os.getenv('MQTT_HOST', self.config['mqtt']['mqtt_broker'])
        self.mqtt_port = int(os.getenv('MQTT_PORT', str(self.config['mqtt']['mqtt_port'])))
        self.mqtt_user = os.getenv('MQTT_USER', self.config['mqtt']['mqtt_username'])
        self.mqtt_password = os.getenv('MQTT_PASSWORD', self.config['mqtt']['mqtt_password'])
        self.devicename = self.config['mqtt']['mqtt_topic_prefix']
        self.chunk_size = self.config['mqtt'].get('chunk_size', 8192)
        
        # Configure logging level from config
        log_level = self.config['mqtt'].get('log_level', 'INFO')
        logging.getLogger().setLevel(getattr(logging, log_level))
      
        # Debug log to show which configuration is being used
        logger.debug(f"Using MQTT Configuration from {'environment' if os.getenv('MQTT_HOST') else 'config file'}")
        logger.info(f"MQTT Configuration - Host: {self.mqtt_host}, Port: {self.mqtt_port}")
        
        # Always log MQTT configuration
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
            # Always log stream configuration
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
                "last_silence_time": None,
                "last_silence_duration": None,              
                "audio_reader": AudioStreamReader(
                    chunk_size=self.chunk_size,
                    silence_threshold=station.get('silence_threshold', -50.0),
                    silence_duration=station.get('silence_duration', 15),
                    debounce_time=station.get('debounce_time', 5)
                )
            }
            
            # Always log stream settings
            logger.info(f"Configured {streams[stream_id]['name']} with:")
            logger.info(f"  Silence threshold: {streams[stream_id]['silence_threshold']} dB")
            logger.info(f"  Silence duration: {streams[stream_id]['silence_duration']} seconds")
            logger.info(f"  Debounce time: {streams[stream_id]['debounce_time']} seconds")
        
        logger.info(f"Successfully configured {len(streams)} streams")
        return streams

    def signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        # Always log shutdown
        logger.info("Shutdown signal received")
        self.running = False
        for stream in self.streams.values():
            if 'audio_reader' in stream:
                stream['audio_reader'].stop()

    async def publish_discovery(self, client: Client):
        """Publish Home Assistant MQTT discovery configs with separate state and attribute topics"""
        # Always log discovery start
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
            # Always log individual stream discovery
            logger.info(f"Publishing discovery config for stream: {stream_id}")

            status_config = {
                "name": f"{stream['name']} Status",
                "state_topic": f"{self.devicename}/sensor/{stream_id}/state",
                "value_template": "{{ value_json.state }}",
                "unique_id": f"{device_id}_{stream_id}_status",
                "object_id": f"{device_id}_{stream_id}_status",
                "availability_topic": f"{self.devicename}/sensor/{stream_id}/availability",
                "device_class": "connectivity",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_config,
                "icon": "mdi:radio",
                "json_attributes_topic": f"{self.devicename}/sensor/{stream_id}/attributes"
            }

            # Debug level - detailed config info
            logger.debug(f"Publishing status sensor config for {stream_id}")
            await client.publish(
                f"{base_topic}/binary_sensor/{stream_id}/status/config",
                payload=json.dumps(status_config).encode(),
                qos=1,
                retain=True
            )

            silence_config = {
                "name": f"{stream['name']} Silence",
                "state_topic": f"{self.devicename}/sensor/{stream_id}/state",
                "value_template": "{{ value_json.silence }}",
                "unique_id": f"{device_id}_{stream_id}_silence",
                "object_id": f"{device_id}_{stream_id}_silence",
                "availability_topic": f"{self.devicename}/sensor/{stream_id}/availability",
                "device_class": "problem",
                "payload_on": "ON",
                "payload_off": "OFF",
                "device": device_config,
                "icon": "mdi:volume-off",
                "json_attributes_topic": f"{self.devicename}/sensor/{stream_id}/attributes"
            }

            logger.debug(f"Publishing silence sensor config for {stream_id}")
            await client.publish(
                f"{base_topic}/binary_sensor/{stream_id}/silence/config",
                payload=json.dumps(silence_config).encode(),
                qos=1,
                retain=True
            )

            # Publish availability
            await client.publish(
                f"{self.devicename}/sensor/{stream_id}/availability",
                payload="online",
                qos=1,
                retain=True
            )

    async def update_sensor_state(self, client: Client, stream_id: str, online: bool, silent: Optional[bool] = None, level_db: Optional[float] = None):
        """Update sensor states and attributes, publishing separate MQTT messages for each"""
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
                stream['silence_start'] = None  # Reset silence timing on offline
                logger.info(f"Stream {stream_id} is now offline")

        # Update silence state and timing
        if online and silent is not None and silent != stream['silent']:
            stream['silent'] = silent
            if silent:
                stream['silence_start'] = now
                logger.info(f"Silence detected on stream {stream_id}")
            else:
                # Update historical values when silence ends
                if stream['silence_start']:
                    stream['last_silence_time'] = stream['silence_start']
                    stream['last_silence_duration'] = (now - stream['silence_start']).total_seconds()
                    logger.info(f"Audio resumed after {stream['last_silence_duration']:.1f} seconds of silence")
                stream['silence_start'] = None

        # Calculate current silence duration if in silent state
        current_silence_duration = None
        if stream['silent'] and stream['silence_start']:
            current_silence_duration = (now - stream['silence_start']).total_seconds()

        # State message - Essential state
        state_payload = {
            'state': 'ON' if online else 'OFF',
            'silence': 'ON' if silent else 'OFF'
        }

        # Attribute message with historical data
        attr_payload = {
            'online_since': stream['online_start'].isoformat() if stream['online_start'] else None,
            'offline_since': stream['offline_start'].isoformat() if stream['offline_start'] else None,
            'silence_since': stream['silence_start'].isoformat() if stream['silence_start'] else None,
            'current_silence_duration': round(current_silence_duration, 1) if current_silence_duration is not None else None,
            'last_silence_time': stream['last_silence_time'].isoformat() if 'last_silence_time' in stream and stream['last_silence_time'] else None,
            'last_silence_duration': round(stream['last_silence_duration'], 1) if 'last_silence_duration' in stream and stream['last_silence_duration'] is not None else None,
            'level_db': float(round(level_db, 2)) if level_db is not None else None,
            'last_update': now.isoformat()
        }

        # Publish state
        logger.debug(f"Publishing state for {stream_id}: {state_payload}")
        await client.publish(
            f"{self.devicename}/sensor/{stream_id}/state",
            payload=json.dumps(state_payload),
            qos=1,
            retain=True
        )

        # Publish attributes
        logger.debug(f"Publishing attributes for {stream_id}: {attr_payload}")
        await client.publish(
            f"{self.devicename}/sensor/{stream_id}/attributes",
            payload=json.dumps(attr_payload),
            qos=1,
            retain=True
        )

    async def check_stream(self, client: Client, stream_id: str):
        """Check a single stream's status and silence"""
        stream = self.streams[stream_id]
        # Always log stream checks
        logger.info(f"Checking stream {stream_id} ({stream['name']}) at {stream['url']}")
        
        try:
            # Check stream status and audio levels
            is_silent, level_db, format_info = await stream['audio_reader'].read_stream(stream['url'])
            
            if level_db is not None:  # Stream is accessible
                if is_silent:
                    # Always log silence status
                    logger.info(f"Stream {stream_id} is available but silent (Level: {level_db:.2f}dB)")
                    await self.update_sensor_state(client, stream_id, True, True, level_db)
                else:
                    # Always log audio status
                    logger.info(f"Stream {stream_id} is available with audio detected (Level: {level_db:.2f}dB)")
                    await self.update_sensor_state(client, stream_id, True, False, level_db)
            else:
                logger.warning(f"Stream {stream_id} is not accessible")
                await self.update_sensor_state(client, stream_id, False)
            
        except Exception as e:
            logger.error(f"Error checking stream {stream_id}: {e}")
            await self.update_sensor_state(client, stream_id, False)

    async def monitor_streams(self):
        """Main monitoring loop"""
        try:
            # Always log connection attempts
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
                    # Publish discovery configs for all stations
                    await self.publish_discovery(client)
                    # Update availability for each stream
                    for stream_id in self.streams:
                        await client.publish(
                            f"{self.devicename}/sensor/{stream_id}/availability",
                            payload="online",
                            qos=1,
                            retain=True
                        )
                    
                    # Always log monitoring cycles
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
   
