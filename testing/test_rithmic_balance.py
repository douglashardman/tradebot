#!/usr/bin/env python3
"""
Test Rithmic API connection and account balance query.

Run with credentials in .env:
    PYTHONPATH=. python testing/test_rithmic_balance.py

Expected .env variables:
    RITHMIC_USER=your_username
    RITHMIC_PASSWORD=your_password
    RITHMIC_ACCOUNT_ID=your_account_id (optional)
"""

import asyncio
import os
import sys
from pathlib import Path

# Load environment
from dotenv import load_dotenv
load_dotenv()


async def test_with_adapter():
    """Test balance query using our RithmicAdapter wrapper."""
    print("\n" + "=" * 60)
    print("Testing via RithmicAdapter")
    print("=" * 60)

    from src.data.adapters.rithmic import RithmicAdapter

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")
    account_id = os.getenv("RITHMIC_ACCOUNT_ID")

    if not user or not password:
        print("ERROR: RITHMIC_USER and RITHMIC_PASSWORD required in .env")
        return None

    print(f"User: {user}")
    print(f"Account ID: {account_id or '(will auto-detect)'}")

    adapter = RithmicAdapter(
        user=user,
        password=password,
        system_name="Rithmic Paper Trading",  # or "Rithmic Test"
        account_id=account_id,
    )

    print("\nConnecting to Rithmic...")
    connected = await adapter.connect()

    if not connected:
        print("ERROR: Failed to connect to Rithmic")
        return None

    print("Connected successfully!")

    # Query balance
    print("\nQuerying account balance...")
    balance = await adapter.get_account_balance(account_id)

    if balance is not None:
        print(f"\n✓ Account Balance: ${balance:,.2f}")
    else:
        print("\n✗ Could not retrieve balance (method may not be supported)")

    # Clean up
    await adapter.disconnect()

    return balance


async def test_direct_client():
    """Test directly with async_rithmic to discover available methods."""
    print("\n" + "=" * 60)
    print("Testing direct async_rithmic client")
    print("=" * 60)

    try:
        from async_rithmic import RithmicClient
    except ImportError:
        print("ERROR: async_rithmic not installed")
        print("Install with: pip install async-rithmic")
        return None

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")
    account_id = os.getenv("RITHMIC_ACCOUNT_ID")
    server = os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443")
    system_name = os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Paper Trading")

    if not user or not password:
        print("ERROR: RITHMIC_USER and RITHMIC_PASSWORD required in .env")
        return None

    print(f"User: {user}")
    print(f"Server: {server}")
    print(f"System: {system_name}")
    print(f"Account ID: {account_id or '(will auto-detect)'}")

    client = RithmicClient(
        user=user,
        password=password,
        system_name=system_name,
        app_name="BalanceTest",
        app_version="1.0",
        url=server,
    )

    print("\nConnecting...")

    try:
        await client.connect()
        print("Connected!")

        # Discover available methods
        print("\n--- Available client methods ---")
        methods = [m for m in dir(client) if not m.startswith('_') and callable(getattr(client, m, None))]
        for m in sorted(methods):
            print(f"  {m}")

        # Try common account/balance methods
        print("\n--- Attempting balance queries ---")

        # Try get_account_list
        if hasattr(client, 'get_account_list'):
            print("\nTrying get_account_list()...")
            try:
                accounts = await client.get_account_list()
                print(f"  Accounts: {accounts}")
            except Exception as e:
                print(f"  Error: {e}")

        # Try get_account_balance
        if hasattr(client, 'get_account_balance'):
            print("\nTrying get_account_balance()...")
            try:
                balance = await client.get_account_balance(account_id)
                print(f"  Balance: {balance}")
            except Exception as e:
                print(f"  Error: {e}")

        # Try get_pnl_position_updates
        if hasattr(client, 'get_pnl_position_updates'):
            print("\nTrying get_pnl_position_updates()...")
            try:
                pnl = await client.get_pnl_position_updates()
                print(f"  PnL data: {pnl}")
            except Exception as e:
                print(f"  Error: {e}")

        # Try subscribe to account updates
        if hasattr(client, 'subscribe_to_account_updates'):
            print("\nTrying subscribe_to_account_updates()...")
            try:
                await client.subscribe_to_account_updates(account_id)
                print("  Subscribed to account updates")
                # Wait briefly for any updates
                await asyncio.sleep(2)
            except Exception as e:
                print(f"  Error: {e}")

    except Exception as e:
        print(f"Connection error: {e}")
        return None
    finally:
        try:
            await client.disconnect()
            print("\nDisconnected.")
        except:
            pass

    return None


async def main():
    print("=" * 60)
    print("Rithmic Balance Test")
    print("=" * 60)

    # Check for credentials
    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")

    if not user or not password:
        print("\nERROR: Missing Rithmic credentials!")
        print("\nSet these in your .env file:")
        print("  RITHMIC_USER=your_username")
        print("  RITHMIC_PASSWORD=your_password")
        print("  RITHMIC_ACCOUNT_ID=your_account_id (optional)")
        sys.exit(1)

    # Test direct client first to discover API
    await test_direct_client()

    # Then test our adapter
    balance = await test_with_adapter()

    print("\n" + "=" * 60)
    print("Test Complete")
    print("=" * 60)

    if balance is not None:
        print(f"\nFinal balance: ${balance:,.2f}")
    else:
        print("\nBalance query not successful - check output above for details")


if __name__ == "__main__":
    asyncio.run(main())
