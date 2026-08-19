# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio

from src.jupyter_client import jupyter_manager


async def kernel_reaper_loop():
    """
    Runs forever. Checks for stale kernels every 5 minutes.
    """
    try:
        while True:
            # Run the prune logic
            # Set max_age_seconds to 3600 (1 hour)
            await jupyter_manager.prune_stale_kernels(max_age_seconds=3600)

            # Sleep for 5 minutes before checking again
            await asyncio.sleep(300)
    except asyncio.CancelledError:
        # Handle clean shutdown if needed
        print("Reaper task cancelled.")


