# CameraService

A minimal Python service that connects to a traffic camera using the vendor
NetSDK, subscribes to traffic junction events and persists captured data.

* Car images and cropped plate images are stored under `images/`.
* Event metadata is written to a SQLite database specified by `DB_PATH`.
* If the database is unavailable, events are processed normally and a warning is logged.
* The service exposes a `process_event` hook for further custom logic.

## Running

Configure connection parameters through environment variables:

```
CAMERA_HOST=127.0.0.1
CAMERA_PORT=37777
CAMERA_USER=admin
CAMERA_PASS=admin
CAMERA_CHANNEL=0
DB_PATH=/path/to/events.db
```

Start the service:

```
python camera_service.py
```

The service will keep running and logging events until interrupted.
