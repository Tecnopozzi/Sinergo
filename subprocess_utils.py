"""Attesa dei worker con rilascio del processo anche se il chiamante viene cancellato."""

import asyncio
import contextlib


async def communicate_with_timeout(proc: asyncio.subprocess.Process, timeout: float):
    communication = asyncio.create_task(proc.communicate())
    try:
        return await asyncio.wait_for(asyncio.shield(communication), timeout=timeout)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        # Il drain resta vivo anche dopo il timeout: wait() da solo puo' restare
        # bloccato se il trasporto subprocess conserva dati nelle pipe.
        try:
            await communication
        finally:
            await proc.wait()
