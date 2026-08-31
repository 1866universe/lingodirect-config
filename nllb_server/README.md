
# NLLB-200 Translation Server

Lightweight, self-hosted REST API and Admin Dashboard for language translation powered by CTranslate2 and Meta's NLLB-200 model.

## Requirements
- Python 3.9+
- NVIDIA GPU with CUDA (recommended) or CPU

## Installation
    pip install -r requirements.txt

## Model Setup
Place your CTranslate2-converted NLLB model folder inside the models/ directory or specify a custom path using NLLB_MODEL_PATH.

## Running the Server
    python app.py

By default, the server runs on http://localhost:5000.

## Environment Variables
- ADMIN_PASSWORD: Master password for the web dashboard.
- NLLB_MODEL_PATH: Custom path to the model directory.
- LOG_DIR: Path to store log files.
- CONFIG_PATH: Path to the config file.