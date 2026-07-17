#!/usr/bin/env python3

import asyncio
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from pyeconet import EcoNetApiInterface, EquipmentType
from pyeconet.equipment.water_heater import WaterHeaterOperationMode

# Mode aliases for easy use
MODES = {
    "energy_saver": WaterHeaterOperationMode.ENERGY_SAVING,
    "high_demand": WaterHeaterOperationMode.HIGH_DEMAND,
    "heat_pump": WaterHeaterOperationMode.HEAT_PUMP_ONLY,
    "vacation": WaterHeaterOperationMode.VACATION,
    "off": WaterHeaterOperationMode.OFF,
}

# Reverse lookup
MODE_NAMES = {v: k for k, v in MODES.items()}


def _get_creds():
    email = os.environ.get("RHEEM_EMAIL")
    password = os.environ.get("RHEEM_PASSWORD")
    if not email or not password:
        raise RuntimeError("Set RHEEM_EMAIL and RHEEM_PASSWORD environment variables")
    return email, password


async def _get_status():
    email, password = _get_creds()
    api = await EcoNetApiInterface.login(email, password)
    equipment = await api.get_equipment_by_type([EquipmentType.WATER_HEATER])
    heaters = equipment.get(EquipmentType.WATER_HEATER, [])

    results = []
    for wh in heaters:
        results.append(
            {
                "name": wh.device_name,
                "mode": wh.mode.name if wh.mode else "Unknown",
                "mode_key": MODE_NAMES.get(wh.mode, "unknown"),
                "running": wh.running,
                "set_point": wh.set_point,
                "hot_water_pct": wh.tank_hot_water_availability,
            }
        )
    return results


async def _set_mode(mode_key):
    if mode_key not in MODES:
        raise ValueError(f"Unknown mode: {mode_key}. Options: {list(MODES.keys())}")

    email, password = _get_creds()
    api = await EcoNetApiInterface.login(email, password)
    equipment = await api.get_equipment_by_type([EquipmentType.WATER_HEATER])
    heaters = equipment.get(EquipmentType.WATER_HEATER, [])

    if not heaters:
        raise RuntimeError("No water heaters found")

    api.subscribe()
    await asyncio.sleep(1)

    for wh in heaters:
        old_mode = wh.mode.name if wh.mode else "Unknown"
        wh.set_mode(MODES[mode_key])

    await asyncio.sleep(2)
    api.unsubscribe()

    return {"name": heaters[0].device_name, "old_mode": old_mode, "new_mode": mode_key}


# === Synchronous API (use these) ===


def status():
    """Get water heater status. Returns list of dicts."""
    return asyncio.run(_get_status())


def set_mode(mode_key):
    """Set mode. mode_key is one of: energy_saver, high_demand, heat_pump, vacation, off"""
    return asyncio.run(_set_mode(mode_key))


def set_high_demand():
    """Convenience: switch to high demand mode."""
    return set_mode("high_demand")


def set_energy_saver():
    """Convenience: switch to energy saver mode."""
    return set_mode("energy_saver")


# Test
if __name__ == "__main__":
    print("Status:", status())
