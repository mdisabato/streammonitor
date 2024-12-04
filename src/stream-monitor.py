import aiohttp
import asyncio
import numpy as np
from pydub import AudioSegment
import yaml
import io
import paho.mqtt.client as mqtt
import time

# MQTT Client setup
mqtt_client = None

def setup_mqtt(config):
    global mqtt_client
    mqtt_client = mqtt.Client()
    mqtt_client.username_pw_set(config["mqtt"]["mqtt_username"], config["mqtt"]["mqtt_password"])
    mqtt_client.connect(config["mqtt"]["mqtt_broker"], config["mqtt"]["mqtt_port"])
    mqtt_client.loop_start()


async def monitor_stream(station, mqtt_topic_prefix):
    url = station["station_url"]
    silence_threshold = station.get("silence_threshold", -50.0)  # in dB
    silence_duration = station.get("silence_duration", 15)  # in seconds
    chunk_size = station.get("chunk_size", 4096)  # bytes
    debounce_time = station.get("debounce_time", 5)  # seconds to confirm silence
    station_name = station["station_name"]

    topic = f"{mqtt_topic_prefix}/{station_name}"

    silence_start_time = None
    last_state = None

    print(f"Monitoring: {station_name} at {url}")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    mqtt_client.publish(f"{topic}/status", "offline", retain=True)
                    print(f"Stream is offline: {station_name} ({url})")
                    return

                mqtt_client.publish(f"{topic}/status", "online", retain=True)

                buffer = b""
                while True:
                    chunk = await response.content.read(chunk_size)
                    if not chunk:
                        mqtt_client.publish(f"{topic}/status", "disconnected", retain=True)
                        print(f"Stream disconnected: {station_name} ({url})")
                        break

                    buffer += chunk
                    if len(buffer) > chunk_size * 10:  # Process data periodically
                        try:
                            audio = AudioSegment.from_file(io.BytesIO(buffer), format="mp3")
                        except Exception as e:
                            print(f"Error processing audio: {e}")
                            buffer = b""
                            continue

                        rms = audio.rms  # Root Mean Square
                        db = 20 * np.log10(rms) if rms > 0 else -float('inf')

                        if db < silence_threshold:
                            if silence_start_time is None:
                                silence_start_time = asyncio.get_event_loop().time()
                            elif asyncio.get_event_loop().time() - silence_start_time > silence_duration:
                                if last_state != "silence":
                                    mqtt_client.publish(f"{topic}/silence", "detected", retain=True)
                                    print(f"Silence detected on {station_name} for {silence_duration} seconds.")
                                    last_state = "silence"
                        else:
                            silence_start_time = None  # Reset silence timer
                            if last_state != "active":
                                mqtt_client.publish(f"{topic}/silence", "cleared", retain=True)
                                print(f"Audio active on {station_name}.")
                                last_state = "active"

                        buffer = b""  # Clear the buffer
    except Exception as e:
        mqtt_client.publish(f"{topic}/error", str(e), retain=True)
        print(f"Error monitoring stream {station_name} ({url}): {e}")


async def main():
    # Load configuration from YAML file
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    setup_mqtt(config)

    tasks = []
    mqtt_topic_prefix = config["mqtt"]["mqtt_topic_prefix"]

    for station in config["stations"]:
        tasks.append(monitor_stream(station, mqtt_topic_prefix))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
