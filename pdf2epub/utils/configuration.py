"""Configuration loading and workflow policy validation."""
from pathlib import Path
from typing import Dict
import yaml
from pdf2epub.pipeline_policy import PipelinePolicy

def load_config(config_path: str = "config.yaml") -> Dict:
    """Load the current nested YAML configuration without migration."""
    with Path(config_path).open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}
    from pdf2epub.ocr_consensus import validate_ocr_config

    validate_ocr_config(config)
    PipelinePolicy.from_config(config)
    return config
