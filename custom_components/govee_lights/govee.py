"""
Govee generic helpers.

GoveeHelper - scene download, parsing, and effect list building.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, cast

import requests

_LOGGER = logging.getLogger(__name__)

_GOVEE_SCENE_DOWNLOAD_TIMEOUT: int = 10  # seconds for scene data HTTP requests


class GoveeHelper:
    """Generic Govee helpers: scene download, parsing, and effect list building."""

    # ── Model capability flags ───────────────────────────────────────────────
    # Models that encode RGB colour in a segmented SEGMENTS (0x15 0x01 …) BLE packet format.
    SEGMENTED_MODELS: list[str] = ["H6053", "H6072", "H6102", "H6199", "H617A", "H617C"]
    # Models that express brightness as a 0-100 percentage rather than 0-255.
    PERCENT_MODELS: list[str] = ["H617A", "H617C"]
    # Models that use mode byte 0x0D instead of 0x02 for colour/CT BLE packets.
    COLOUR_D_MODELS: list[str] = ["H6005", "H6052", "H6058", "H6102", "H613B", "H613D", "H617E"]

    @staticmethod
    def build_ptreal_cmds(scene_code: int, scence_param: str) -> list[str]:
        """Encode a scene into pre-built BLE/ptReal packet frames (base64-encoded 20-byte packets).
        Mirrors SetSceneCode::encode from govee2mqtt/src/ble.rs."""
        payload = base64.b64decode(scence_param)
        raw = bytearray([0xa3, 0x00, 0x01, 0x00, 0x02])  # header; byte[3] patched below
        num_lines = 0
        last_line_marker = 1
        for b in payload:
            if len(raw) % 19 == 0:
                num_lines += 1
                raw.append(0xa3)
                last_line_marker = len(raw)
                raw.append(num_lines)
            raw.append(b)
        raw[last_line_marker] = 0xFF    # mark last data line
        raw[3] = num_lines + 1          # total frame count
        packets: list[bytes] = []
        for i in range(0, len(raw), 19):
            chunk = bytes(raw[i: i + 19])
            padded = chunk + bytes(19 - len(chunk))
            xor = 0
            for byte in padded:
                xor ^= byte
            packets.append(padded + bytes([xor]))
        lo = scene_code & 0xFF
        hi = (scene_code >> 8) & 0xFF
        code_pkt = bytes([0x33, 0x05, 0x04, lo, hi]) + bytes(14)
        xor = 0
        for byte in code_pkt:
            xor ^= byte
        packets.append(code_pkt + bytes([xor]))
        return [base64.b64encode(p).decode() for p in packets]

    @staticmethod
    def parse_api_scene_response(model: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse a Govee API response into the flat scene list format,
        selecting the model-specific specialEffect scenceParam where available."""
        scenes: list[dict[str, Any]] = []
        for cat in data["data"]["categories"]:
            cat_name: str = cat["categoryName"]
            for scene in cat["scenes"]:
                for effect in scene["lightEffects"]:
                    code: int = effect["sceneCode"]
                    param: str = effect.get("scenceParam", "")
                    for spe in effect.get("specialEffect", []):
                        if model in spe.get("supportSku", []):
                            param = spe["scenceParam"]
                            break
                    if not param:
                        continue
                    ptreal = (
                        GoveeHelper.build_ptreal_cmds(code, param) if code != 0 else []
                    )
                    scenes.append({
                        "category": cat_name,
                        "scene_name": scene["sceneName"],
                        "scene_id": scene["sceneId"],
                        "scene_code": code,
                        "scence_param": param,
                        "ptreal_cmds": ptreal,
                    })
        return scenes

    @staticmethod
    def download_model_scenes(model: str, config_dir: str) -> list[dict[str, Any]]:
        """Download scene data from Govee's public light-effect-library endpoint,
        save it to {config_dir}/.storage/govee_lights/{model}.json, and return parsed scenes."""
        url = f"https://app2.govee.com/appsku/v1/light-effect-libraries?sku={model}"
        headers = {
            "AppVersion": "5.6.01",
            "User-Agent": (
                "GoveeHome/5.6.01 (com.ihoment.GoVeeSensor; build:2; iOS 16.5.0) Alamofire/5.6.4"
            ),
        }
        _LOGGER.info("Downloading scene data for %s from Govee API", model)
        resp = requests.get(url, headers=headers, timeout=_GOVEE_SCENE_DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        store_dir = Path(config_dir) / ".storage" / "govee_lights"
        store_dir.mkdir(parents=True, exist_ok=True)
        json_path = store_dir / f"{model}.json"
        json_path.write_text(resp.text)
        _LOGGER.info("Saved scene data for %s to %s", model, json_path)
        return GoveeHelper.parse_api_scene_response(model, resp.json())

    @staticmethod
    def load_model_scenes(model: str, config_dir: str) -> list[dict[str, Any]]:
        """Load scenes from .storage/govee_lights/{model}.json, or download if not present."""
        json_path = Path(config_dir) / ".storage" / "govee_lights" / f"{model}.json"
        if json_path.exists():
            data: Any = json.loads(json_path.read_text())
            if isinstance(data, list):
                _LOGGER.debug("Loaded flat scene data from %s", json_path)
                return cast("list[dict[str, Any]]", data)
            if isinstance(data, dict) and "data" in data:
                _LOGGER.debug("Loaded raw API scene data from %s; parsing", json_path)
                return GoveeHelper.parse_api_scene_response(model, cast("dict[str, Any]", data))
            _LOGGER.debug("Unrecognised format in %s; downloading fresh data", json_path)
        else:
            _LOGGER.debug("No scene file for %s; downloading from Govee API", model)
        return GoveeHelper.download_model_scenes(model, config_dir)

    @staticmethod
    def build_model_effect_list(
        model: str,
        config_dir: str,
    ) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
        """Load and return *(scenes_data, effect_map, effect_list)* for *model*.
        Music-mode pseudo-effects are appended at the end of *effect_list*."""
        scenes = GoveeHelper.load_model_scenes(model, config_dir)
        effect_map: dict[str, int] = {}
        effect_list: list[str] = []
        for idx, scene in enumerate(scenes):
            if not scene.get("ptreal_cmds"):
                continue
            name = scene["category"] + " - " + scene["scene_name"]
            unique_name = name
            counter = 2
            while unique_name in effect_map:
                unique_name = f"{name} ({counter})"
                counter += 1
            effect_map[unique_name] = idx
            effect_list.append(unique_name)
        _LOGGER.debug("Loaded %d effects for model %s", len(effect_list), model)
        return scenes, effect_map, effect_list
