"""
HUB Token — Solana SPL integration for Agent Hub.
Handles: balance checks, airdrops (mint-to), transfers between agents.

Requires:
  - HUB_TOKEN_MINT: SPL token mint address (set after Hands launches token)
  - MINT_AUTHORITY_KEYPAIR: path to keypair JSON with mint authority
  - HUB_WALLET_KEYPAIR: path to Hub's wallet keypair for transfers
"""

import json
import os
import asyncio
from typing import Optional

# Lazy imports — don't crash server if solana libs missing
_SOLANA_AVAILABLE = False
try:
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed
    from solders.pubkey import Pubkey
    from solders.keypair import Keypair
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    _SOLANA_AVAILABLE = True
except ImportError:
    pass

# Config — set these after token launch
HUB_TOKEN_MINT: Optional[str] = os.environ.get(
    "HUB_TOKEN_MINT",
    "9XtsrWuScT28ocG6T4w9dCF3QYtdZabxmG3EgW1Jnhue"  # HUB token mainnet mint
)
SOLANA_RPC = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
MINT_AUTHORITY_PATH = os.environ.get("MINT_AUTHORITY_KEYPAIR", "")
HUB_WALLET_PATH = os.environ.get(
    "HUB_WALLET_KEYPAIR",
    os.path.expanduser("~/.openclaw/workspace/credentials/sol_wallet.json")
)


def is_configured() -> bool:
    """Check if HUB token is fully configured for on-chain operations."""
    return bool(HUB_TOKEN_MINT) and _SOLANA_AVAILABLE


def configure(mint_address: str = "9XtsrWuScT28ocG6T4w9dCF3QYtdZabxmG3EgW1Jnhue",
              mint_authority_path: str = "", rpc_url: str = "", wallet_path: str = ""):
    """Set token configuration at runtime. Defaults to HUB token mainnet mint."""
    global HUB_TOKEN_MINT, MINT_AUTHORITY_PATH, SOLANA_RPC, HUB_WALLET_PATH
    if mint_address:
        HUB_TOKEN_MINT = mint_address
    if mint_authority_path:
        MINT_AUTHORITY_PATH = mint_authority_path
    if rpc_url:
        SOLANA_RPC = rpc_url
    if wallet_path:
        HUB_WALLET_PATH = wallet_path


def _load_keypair(path: str) -> "Keypair":
    """Load a Solana keypair from JSON file."""
    with open(path) as f:
        secret = json.load(f)
    if isinstance(secret, list):
        return Keypair.from_bytes(bytes(secret))
    elif isinstance(secret, str):
        return Keypair.from_base58_string(secret)
    raise ValueError(f"Unknown keypair format in {path}")


async def get_token_balance(wallet_address: str) -> float:
    """Get HUB token balance for a wallet address."""
    if not is_configured():
        return 0.0
    
    async with AsyncClient(SOLANA_RPC) as client:
        mint = Pubkey.from_string(HUB_TOKEN_MINT)
        owner = Pubkey.from_string(wallet_address)
        
        from solana.rpc.types import TokenAccountOpts
        opts = TokenAccountOpts(mint=mint)
        
        resp = await client.get_token_accounts_by_owner_json_parsed(
            owner,
            opts,
            commitment=Confirmed
        )
        
        if resp.value:
            for account in resp.value:
                parsed = account.account.data.parsed
                amount = parsed["info"]["tokenAmount"]["uiAmount"]
                return float(amount) if amount else 0.0
        return 0.0


async def airdrop_hub(recipient_wallet: str, amount: float) -> dict:
    """Mint HUB tokens to a recipient wallet (requires mint authority).
    Used for registration airdrops.
    """
    if not is_configured():
        return {"ok": False, "error": "HUB token not configured (no mint address)"}
    
    if not MINT_AUTHORITY_PATH or not os.path.exists(MINT_AUTHORITY_PATH):
        return {"ok": False, "error": "Mint authority keypair not found"}
    
    # TODO: implement actual SPL mint-to instruction
    # For now, return what the integration will look like
    return {
        "ok": False, 
        "error": "SPL mint-to not yet implemented — waiting for mint address from Hands",
        "would_mint": {
            "recipient": recipient_wallet,
            "amount": amount,
            "mint": HUB_TOKEN_MINT,
            "decimals": 9
        }
    }


async def transfer_hub(from_keypair_path: str, to_wallet: str, amount: float) -> dict:
    """Transfer HUB tokens between wallets.
    Used for bounty payouts and Trust Olympics escrow settlements.
    """
    if not is_configured():
        return {"ok": False, "error": "HUB token not configured"}
    
    if not os.path.exists(from_keypair_path):
        return {"ok": False, "error": f"Keypair not found: {from_keypair_path}"}
    
    try:
        # Load keypair
        with open(from_keypair_path) as f:
            secret = json.load(f)
        pvk = secret.get("private_key", "")
        if isinstance(pvk, str) and len(pvk) == 88:
            import base58
            keypair = Keypair.from_bytes(base58.b58decode(pvk))
        else:
            return {"ok": False, "error": "Unknown keypair format"}
        
        mint_pubkey = Pubkey.from_string(HUB_TOKEN_MINT)
        from_pubkey = keypair.pubkey()
        to_pubkey = Pubkey.from_string(to_wallet)
        
        async with AsyncClient(SOLANA_RPC) as client:
            # Get or create source ATA
            from spl.token.instructions import (
                get_associated_token_address,
                create_associated_token_account,
                transfer as spl_transfer,
                TransferParams,
                ACCOUNT_LEN,
                TOKEN_PROGRAM_ID,
                ASSOCIATED_TOKEN_PROGRAM_ID,
            )
            
            source_ata = get_associated_token_address(from_pubkey, mint_pubkey)
            dest_ata = get_associated_token_address(to_pubkey, mint_pubkey)
            
            # Check if dest ATA exists, if not create it
            resp = await client.get_account_info_json_parsed(dest_ata, commitment=Confirmed)
            if not resp.value:
                # Create destination ATA
                create_ata_ix = create_associated_token_account(
                    payer=from_pubkey,
                    owner=to_pubkey,
                    mint=mint_pubkey,
                )
            else:
                create_ata_ix = None
            
            # Build transfer instruction
            # Amount in lamports (SPL tokens use 9 decimals)
            amount_lamports = int(amount * 1e9)
            
            transfer_ix = spl_transfer(TransferParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                dest=dest_ata,
                owner=from_pubkey,
                amount=amount_lamports,
                signers=[],
            ))
            
            # Build transaction
            from solders.transaction import Transaction
            from solders.system_program import CreateAccountParams, CreateAccount
            
            tx = Transaction()
            if create_ata_ix:
                tx.add(create_ata_ix)
            tx.add(transfer_ix)
            
            # Sign and send
            recent = await client.get_latest_blockhash(commitment=Confirmed)
            tx.recent_blockhash = recent.value.blockhash
            
            tx.sign(keypair, recent.value.blockhash)
            sig = await client.send_transaction(tx, keypair, commitment=Confirmed)
            
            # Wait for confirmation
            await client.confirm_transaction(sig.value, commitment=Confirmed)
            
            return {
                "ok": True,
                "signature": str(sig.value),
                "from": str(from_pubkey),
                "to": to_wallet,
                "amount": amount,
                "mint": HUB_TOKEN_MINT,
                "dest_ata": str(dest_ata),
            }
            
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Sync wrappers for Flask
def get_balance_sync(wallet_address: str) -> float:
    return asyncio.run(get_token_balance(wallet_address))

def airdrop_sync(recipient_wallet: str, amount: float) -> dict:
    return asyncio.run(airdrop_hub(recipient_wallet, amount))

def transfer_sync(from_keypair_path: str, to_wallet: str, amount: float) -> dict:
    return asyncio.run(transfer_hub(from_keypair_path, to_wallet, amount))
