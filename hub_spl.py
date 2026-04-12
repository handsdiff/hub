"""
HUB SPL Token Transfer Module
Handles on-chain HUB token transfers for bounties, airdrops, etc.
Error classification: retriable vs permanent.
"""

import json, struct, os, re
import base58
from solana.rpc.api import Client
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.instruction import Instruction, AccountMeta
from solders.transaction import Transaction
from solders.message import Message
from solders.compute_budget import set_compute_unit_price

HUB_MINT = Pubkey.from_string("9XtsrWuScT28ocG6T4w9dCF3QYtdZabxmG3EgW1Jnhue")
TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ASSOCIATED_TOKEN_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
SYSTEM_PROGRAM = Pubkey.from_string("11111111111111111111111111111111")
RENT = Pubkey.from_string("SysvarRent111111111111111111111111111111111")

# HUB has 6 decimals
HUB_DECIMALS = 6

# Load wallet — try hub-wallet-v2.json first (base58 string), then hub-wallet.json (list bytes)
_wallet_paths = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub-wallet-v2.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub-wallet.json"),
    os.path.expanduser("~/.openclaw/credentials/hub-wallet-v2.json"),
    os.path.expanduser("~/.openclaw/credentials/hub-wallet.json"),
]
WALLET_KP = None
WALLET_PUBKEY = None
for _wp in _wallet_paths:
    if os.path.exists(_wp):
        try:
            with open(_wp) as f:
                _kd = json.load(f)
            _pk = _kd.get("private_key") or _kd.get("privateKey")
            if isinstance(_pk, str):
                WALLET_KP = Keypair.from_base58_string(_pk)
            elif isinstance(_pk, list):
                WALLET_KP = Keypair.from_bytes(bytes(_pk))
            WALLET_PUBKEY = WALLET_KP.pubkey()
            print(f"[HUB-SPL] Loaded wallet from {_wp}: {WALLET_PUBKEY}")
            break
        except Exception as e:
            print(f"[HUB-SPL] Failed to load wallet from {_wp}: {e}")
if WALLET_KP is None:
    print("[HUB-SPL] WARNING: No wallet keypair found — SPL transfers will fail. Set up wallet at credentials/hub-wallet[-v2].json")

SOLANA_RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
CLIENT = Client(SOLANA_RPC_URL)

# ─── Error classification ────────────────────────────────────────────────────

def _classify_error(exc: Exception) -> str:
    """
    Classify an exception as 'retriable' or 'permanent'.
    Retriable: transient network/RPC failures likely to succeed on retry.
    Permanent: application-level failures that will not succeed on retry.
    """
    msg = str(exc).lower()

    # Permanent: application-level errors that won't change on retry
    if "insufficient funds" in msg:
        return "permanent"
    if "invalid" in msg and ("address" in msg or "pubkey" in msg or "base58" in msg):
        return "permanent"
    if "wrong mint" in msg or "incorrect program id" in msg or "incorrect program" in msg:
        return "permanent"
    if "token account" in msg and "not found" in msg:
        # ATA doesn't exist and creation failed — unlikely to self-correct
        return "permanent"
    if "already exists" in msg and "token account" in msg:
        # Race condition on ATA creation — safe to retry
        return "retriable"
    if "blockhash not found" in msg:
        return "retriable"
    if "too old" in msg:
        return "retriable"

    # Retriable: transient RPC/network failures
    if "timeout" in msg or "timed out" in msg:
        return "retriable"
    if "connection" in msg and ("refused" in msg or "reset" in msg or "timeout" in msg):
        return "retriable"
    if "429" in msg or "rate limit" in msg or "rate_limit" in msg:
        return "retriable"
    if "503" in msg or "service unavailable" in msg:
        return "retriable"
    if "bad gateway" in msg:
        return "retriable"
    if "econnreset" in msg or "enetunreach" in msg or "network" in msg:
        return "retriable"

    # Default: permanent for unknown exceptions (safer — don't infinite retry)
    return "permanent"


# ─── Token helpers ──────────────────────────────────────────────────────────

def get_ata(owner: Pubkey, mint: Pubkey = HUB_MINT) -> Pubkey:
    """Derive associated token account address."""
    seeds = [bytes(owner), bytes(TOKEN_PROGRAM), bytes(mint)]
    ata, _ = Pubkey.find_program_address(seeds, ASSOCIATED_TOKEN_PROGRAM)
    return ata


def create_ata_instruction(payer: Pubkey, owner: Pubkey, mint: Pubkey = HUB_MINT) -> Instruction:
    """Create associated token account instruction."""
    ata = get_ata(owner, mint)
    return Instruction(
        program_id=ASSOCIATED_TOKEN_PROGRAM,
        accounts=[
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(ata, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=False, is_writable=False),
            AccountMeta(mint, is_signer=False, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM, is_signer=False, is_writable=False),
            AccountMeta(TOKEN_PROGRAM, is_signer=False, is_writable=False),
        ],
        data=b'',
    )


def spl_transfer_instruction(source: Pubkey, dest: Pubkey, owner: Pubkey, amount_raw: int) -> Instruction:
    """SPL Token transfer instruction."""
    data = struct.pack('<BQ', 3, amount_raw)  # instruction index 3 = Transfer
    return Instruction(
        program_id=TOKEN_PROGRAM,
        accounts=[
            AccountMeta(source, is_signer=False, is_writable=True),
            AccountMeta(dest, is_signer=False, is_writable=True),
            AccountMeta(owner, is_signer=True, is_writable=False),
        ],
        data=data,
    )


def ata_exists(ata: Pubkey) -> bool:
    """Check if an associated token account exists."""
    resp = CLIENT.get_account_info(ata)
    return resp.value is not None


def get_hub_balance(owner_address: str) -> float:
    """Get HUB balance for an address."""
    try:
        owner = Pubkey.from_string(owner_address)
        ata = get_ata(owner)
        resp = CLIENT.get_token_account_balance(ata)
        if resp.value:
            return resp.value.ui_amount or 0.0
    except Exception:
        pass
    return 0.0


# ─── Core transfer ───────────────────────────────────────────────────────────

def send_hub(recipient_address: str, amount: float) -> dict:
    """
    Send HUB tokens to a recipient.
    Creates ATA if needed. Returns tx signature or structured error.

    Returns:
        {"success": True, "signature": "...", "amount": 100.0, "error_type": None}
        or {"success": False, "error": "...", "error_type": "retriable"|"permanent"}

    Error classification:
        retriable  — RPC timeout, rate limit, network blip. Safe to retry with backoff.
        permanent   — insufficient funds, invalid recipient, wrong mint. Do not retry.
    """
    try:
        recipient = Pubkey.from_string(recipient_address)
    except Exception as exc:
        return {
            "success": False,
            "error": f"Invalid recipient address: {exc}",
            "error_type": "permanent",
        }

    try:
        amount_raw = int(amount * (10 ** HUB_DECIMALS))
    except Exception as exc:
        return {
            "success": False,
            "error": f"Invalid amount: {exc}",
            "error_type": "permanent",
        }

    try:
        source_ata = get_ata(WALLET_PUBKEY)
        dest_ata = get_ata(recipient)

        instructions = []

        # Priority fee for reliable inclusion
        instructions.append(set_compute_unit_price(50_000))

        # Create ATA if it doesn't exist
        if not ata_exists(dest_ata):
            instructions.append(create_ata_instruction(WALLET_PUBKEY, recipient))

        # Transfer
        instructions.append(spl_transfer_instruction(source_ata, dest_ata, WALLET_KP, amount_raw))

        # Build and send transaction
        recent = CLIENT.get_latest_blockhash()
        msg = Message.new_with_blockhash(instructions, WALLET_PUBKEY, recent.value.blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([WALLET_KP], recent.value.blockhash)

        result = CLIENT.send_transaction(tx)
        sig = str(result.value)

        return {
            "success": True,
            "signature": sig,
            "amount": amount,
            "recipient": recipient_address,
            "solscan": f"https://solscan.io/tx/{sig}",
            "error_type": None,
        }
    except Exception as exc:
        etype = _classify_error(exc)
        return {
            "success": False,
            "error": str(exc),
            "error_type": etype,
        }


def get_treasury_balance() -> float:
    """Get Brain's HUB treasury balance."""
    return get_hub_balance(str(WALLET_PUBKEY))


if __name__ == "__main__":
    print(f"Wallet: {WALLET_PUBKEY}")
    print(f"HUB Mint: {HUB_MINT}")
    print(f"Treasury: {get_treasury_balance()} HUB")
