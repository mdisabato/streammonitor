# AzuraCast Stream Monitor

A lightweight Python service that monitors AzuraCast radio streams for availability and silence, publishing status to MQTT with Home Assistant integration.

## Features

- Monitors multiple AzuraCast streams simultaneously
- Detects stream availability and audio silence
- Publishes status via MQTT with Home Assistant auto-discovery
- Docker support with Alpine Linux for minimal resource usage

## Requirements

- Docker
- MQTT Broker (e.g., Mosquitto)
- Home Assistant (optional, for dashboard integration)

## Installation

1. Clone this repository:
```bash
git clone https://github.com/yourusername/azuracast-stream-monitor.git
cd azuracast-stream-monitor
```

2. Configure your streams in `config/streams.json`:
```json
{
    "christmas_music": {
        "url": "http://your.server:8220/radio.mp3",
        "name": "Christmas Music"
    }
}
```

3. Build the Docker container:
```bash
docker build -t stream-monitor .
```

4. Run the container:
```bash
docker run -d \
    --name stream-monitor \
    -v $(pwd)/config:/app/config \
    -e MQTT_HOST=your_mqtt_host \
    -e MQTT_USER=your_mqtt_user \
    -e MQTT_PASSWORD=your_mqtt_password \
    stream-monitor
```

## Configuration

### Environment Variables

- `MQTT_HOST`: MQTT broker hostname
- `MQTT_PORT`: MQTT broker port (default: 1883)
- `MQTT_USER`: MQTT username
- `MQTT_PASSWORD`: MQTT password

### Stream Configuration

Configure your streams in `config/streams.json`. Example:
```json
{
    "stream_id": {
        "url": "http://stream.url:port/mount.mp3",
        "name": "Display Name"
    }
}
```

## Home Assistant Integration

The monitor automatically creates the following entities for each stream:

- `binary_sensor.azuracast_[stream_id]_status`: Online/offline status
- `binary_sensor.azuracast_[stream_id]_silence`: Silence detection

## Docker Compose

Example docker-compose.yml:
```yaml
version: '3'
services:
  stream-monitor:
    build: .
    container_name: stream-monitor
    restart: unless-stopped
    volumes:
      - ./config:/app/config
    environment:
      - MQTT_HOST=mqtt.your.domain
      - MQTT_USER=your_username
      - MQTT_PASSWORD=your_password
```

## License

MIT License - see LICENSE file for details
