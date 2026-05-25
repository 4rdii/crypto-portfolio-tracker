"""Seed preset rebalancing strategies into the database."""
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "portfolio.db"

PRESET_STRATEGIES = [
    {
        "name": "Conservative",
        "allocations": {
            "USDC": 60,
            "BTC": 25,
            "ETH": 15
        }
    },
    {
        "name": "Balanced",
        "allocations": {
            "BTC": 40,
            "ETH": 30,
            "SOL": 15,
            "USDC": 15
        }
    },
    {
        "name": "AI & Tech",
        "allocations": {
            "NVDAx": 20,
            "MSFTx": 15,
            "GOOGLx": 10,
            "SOL": 15,
            "ETH": 20,
            "USDC": 20
        }
    },
    {
        "name": "$61K Portfolio Plan",
        "allocations": {
            "BTC": 38.0,
            "ETH": 14.2,
            "PAXG": 20.7,
            "USDC": 9.6,
            "SPYx": 4.6,
            "QQQx": 3.1,
            "NVDAx": 2.4,
            "MSFTx": 2.1,
            "GOOGLx": 1.5,
            "AMZNx": 0.7,
            "CPBx": 1.2,
            "STZx": 1.1,
            "HIIx": 0.8
        }
    },
    {
        "name": "Aggressive Growth",
        "allocations": {
            "BTC": 50,
            "ETH": 30,
            "SOL": 10,
            "ORCA": 5,
            "JUP": 5
        }
    },
    {
        "name": "xStocks Only",
        "allocations": {
            "SPYx": 24,
            "QQQx": 16,
            "NVDAx": 12,
            "MSFTx": 11,
            "GOOGLx": 8,
            "AMZNx": 4,
            "CPBx": 6,
            "STZx": 5,
            "HIIx": 4,
            "USDC": 10
        }
    }
]


def seed_presets():
    """Insert preset strategies if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    for preset in PRESET_STRATEGIES:
        # Check if preset with this name already exists
        existing = cursor.execute(
            "SELECT id FROM strategies WHERE name = ? AND is_preset = 1",
            (preset["name"],)
        ).fetchone()

        if existing:
            print(f"✓ Preset '{preset['name']}' already exists (id={existing[0]})")
            continue

        # Insert new preset
        allocations_json = json.dumps(preset["allocations"])
        cursor.execute(
            """INSERT INTO strategies (user_id, name, allocations, is_preset)
               VALUES (NULL, ?, ?, 1)""",
            (preset["name"], allocations_json)
        )
        print(f"✓ Created preset '{preset['name']}' (id={cursor.lastrowid})")

    conn.commit()
    conn.close()
    print(f"\n✓ Seeded {len(PRESET_STRATEGIES)} preset strategies")


if __name__ == "__main__":
    seed_presets()
