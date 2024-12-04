import asyncio
from aiomqtt import Client, MqttError
from datetime import datetime
from audiostreamreader import AudioStreamReader

class StreamMonitor:
    def __init__(self, config, mqtt_client):
        self.config = config
        self.mqtt_client = mqtt_client
        self.streams = self.initialize_streams()

    def initialize_streams(self):
        streams = {}
        for station in self.config['stations']:
            station_id = station['station_name']
            streams[station_id] = {
                "url": station['station_url'],
                "name": station['station_name'],
                "online": False,
                "silent": False,
                "last_check": None,
                "silence_start": None,
                "online_start": None,
                "offline_start": None,
                "audio_reader": AudioStreamReader(),
                "polling_time": station.get('polling_time', 3600),  # Default 3600 seconds
                "silence_duration": station.get('silence_threshold', 15)  # Default 15 seconds
            }
        return streams

    async def check_streams(self):
        while True:
            for station_id, stream in self.streams.items():
                url = stream["url"]
                reader = stream["audio_reader"]
                try:
                    is_silent = await reader.is_silent(url)
                    stream["silent"] = is_silent
                    stream["last_check"] = datetime.utcnow()

                    if is_silent:
                        if stream["silence_start"] is None:
                            stream["silence_start"] = datetime.utcnow()
                        silence_duration = (datetime.utcnow() - stream["silence_start"]).total_seconds()
                        if silence_duration >= stream["silence_duration"]:
                            await self.publish_mqtt(station_id, "silent")
                    else:
                        stream["silence_start"] = None  # Reset silence start time
                        await self.publish_mqtt(station_id, "online")

                except Exception as e:
                    print(f"Error checking stream {station_id}: {e}")
                    await self.publish_mqtt(station_id, "offline")
            await asyncio.sleep(10)  # Adjust monitoring interval as needed

    async def publish_mqtt(self, station_id, status):
        topic = f"stations/{station_id}/status"
        message = status
        try:
            await self.mqtt_client.publish(topic, message, qos=1)
            print(f"Published to MQTT: {topic} - {message}")
        except MqttError as e:
            print(f"MQTT publish error for {station_id}: {e}")

async def main():
    config = {
        "stations": [
            {
                "station_name": "Station1",
                "station_url": "http://example.com/stream1",
                "polling_time": 300,
                "silence_threshold": 15,
            },
            {
                "station_name": "Station2",
                "station_url": "http://example.com/stream2",
            },
        ]
    }

    async with Client("mqtt-broker-address") as mqtt_client:
        monitor = StreamMonitor(config, mqtt_client)
        await monitor.check_streams()

if __name__ == "__main__":
    asyncio.run(main())
