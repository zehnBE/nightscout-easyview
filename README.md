# Nightscout EasyView Uploader

This project provides a **Python-based uploader** that retrieves CGM data from the **Medtrum EasyView**  API and pushes it to a **Nightscout** instance. It is intended for personal use by people who rely on Medtrum devices but want their data available in Nightscout.

## Features

- Login to Medtrum EasyView
- Fetch CGM readings from your Medtrum device
- Fetch pump status from your Medtrum device
- Upload data automatically to a Nightscout server
- Simple configuration and extensible codebase

## Requirements

- A Medtrum Easyview **follower** account that follows exactly 1 user
- A working Nightscout instance
- Python 3.11+ or Docker

## Usage (Python 3.11+)

1. Clone this repository
2. Install `requests` and `pyyaml` dependencies
3. Configure your credentials for Medtrum EasyView and Nightscout (see below) in `~/.nightscout_easyview/secrets.yaml`.
4. Run the uploader:

   ```bash
   python uploader.py
   ```

## Usage (Docker)

1. Clone this repository
2. Configure your credentials for Medtrum EasyView and Nightscout in `docker-compose.yaml` on the Docker host.
3. Create project:

    ```bash
    docker compose up --build -d 
    ```

## Configuration (docker-compose.yaml)

Credentials are read from the Docker environment. The following variables are supported:

Mandatory:
| Variable | Description |
| --- | --- |
| username | Username of the follower account. Likely e-mail address |
| password | Password of the follower acount |
| url | URL of the NightScout instance, e.g. <https://host.domain> |
| secret | NighScout Access Token (at least role "readable") |

Optional:
| Variable | Default |
| --- | --- |
| account_type | auto |
| sync_treatments | true |
| sync_pump_status | true |

## Notes

- This is **not** an official Medtrum or Nightscout project.
- Use at your own risk.
- Contributions are welcome.

## Acknowledgements

Special thanks to [bruderjakob12](https://gist.github.com/bruderjakob12/b492e5c0b32e421d6fc9ee6f86d5f2dd) for his work in reverse engineering parts of the Medtrum EasyView API. His research provided valuable insights that made this uploader possible.
