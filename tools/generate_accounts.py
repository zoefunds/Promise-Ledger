"""One-off helper: generate real, freshly-created accounts for the
Promise Ledger live integration suite and save them as encrypted
keystores under tests/.keys/ (gitignored — never commit these).

Run once: python3 tools/generate_accounts.py
"""

import json
from pathlib import Path

from eth_account import Account

KEYS_DIR = Path(__file__).parent.parent / "tests" / ".keys"
PASSWORD = "pl-live-test-pass-2026"

NAMES = ["submitter_a", "submitter_b", "provider"]


def main() -> None:
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        path = KEYS_DIR / f"{name}.json"
        if path.exists():
            print(f"{name}: already exists, skipping")
            continue
        acct = Account.create()
        keystore = Account.encrypt(acct.key, PASSWORD)
        with open(path, "w") as f:
            json.dump(keystore, f)
        print(f"{name}: {acct.address}")


if __name__ == "__main__":
    main()
