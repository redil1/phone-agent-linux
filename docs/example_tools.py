"""Example PhoneAgent tools. Copy to ~/.config/phone-agent/tools/ and edit.

    mkdir -p ~/.config/phone-agent/tools
    cp docs/example_tools.py ~/.config/phone-agent/tools/my_tools.py

Every file in that directory is imported before each call, and a file is
re-imported when it changes, so you can edit a tool and place another call
without restarting the Studio.

A tool is offered to the model only when the active task contract also lists its
name in `allowed_tools`. That keeps the contract the single place that decides
what a given call is permitted to do.

Three rules the phone imposes:

1. BE FAST. A tool call costs a second model inference before the caller hears
   anything. Past about a second, the caller is listening to silence. Set
   `timeout_secs` to what you can actually promise; on timeout the agent is told
   to admit it could not confirm rather than to guess.
2. NEVER CLAIM MORE THAN YOU DID. Return what actually happened. A handler that
   reports success it did not achieve makes the agent lie to a customer.
3. RETURN SPEAKABLE FACTS. The result is read aloud by a salesperson, not
   rendered in a UI. A short dict beats a database row dump.
"""

from __future__ import annotations

import os
import re

from phone_agent_gateway.ai_bridge.tasks.tool_registry import realtime_tool

# --------------------------------------------------------------------------
# 1. An ACTION the caller asked for.
#
#    This is the tool the agent was already offering ("Would you like to
#    complete checkout?") with nothing behind it. Until it is implemented
#    against a real checkout system, it must not claim a link was sent.
# --------------------------------------------------------------------------


@realtime_tool(
    name="send_checkout_link",
    description=(
        "Send the caller a secure checkout link by SMS for the plan they chose. "
        "Only call this once the caller has clearly agreed to a specific plan."
    ),
    params={
        "plan": {
            "type": "string",
            "enum": ["starter", "professional", "advanced"],
            "description": "The plan the caller agreed to.",
        },
        "phone": {
            "type": "string",
            "description": "Number to text, in the caller's words. Omit to use this call's number.",
        },
    },
    required=["plan"],
    timeout_secs=3.0,
)
async def send_checkout_link(plan: str, phone: str = "") -> dict:
    """Replace the body with a real call to your payment provider."""

    endpoint = os.getenv("CHECKOUT_API_URL", "").strip()
    if not endpoint:
        # Honest failure. The agent will offer to follow up instead of
        # announcing a link the caller will never receive.
        return {
            "sent": False,
            "reason": "checkout_not_configured",
            "say": (
                "Tell the caller you will have a colleague send the checkout link "
                "shortly. Do not say it has already been sent."
            ),
        }

    # import aiohttp
    # async with aiohttp.ClientSession() as session:
    #     async with session.post(
    #         endpoint,
    #         json={"plan": plan, "phone": phone},
    #         headers={"Authorization": f"Bearer {os.environ['CHECKOUT_API_KEY']}"},
    #         timeout=aiohttp.ClientTimeout(total=2.5),
    #     ) as response:
    #         response.raise_for_status()
    #         body = await response.json()
    # return {"sent": True, "plan": plan, "expires_in_minutes": body["expires_in"] // 60}
    raise NotImplementedError("wire this to your checkout provider")


# --------------------------------------------------------------------------
# 2. A DATABASE READ.
#
#    Async so the event loop keeps streaming audio while the query runs.
#    Return the two or three fields worth saying out loud, not the whole row.
# --------------------------------------------------------------------------


@realtime_tool(
    name="lookup_subscriber",
    description=(
        "Check whether this caller already has a subscription, and what it is. "
        "Use it when the caller says they are already a customer."
    ),
    params={"phone": {"type": "string", "description": "Phone number in E.164 form."}},
    required=["phone"],
    timeout_secs=1.5,
)
async def lookup_subscriber(phone: str) -> dict:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8:
        return {"found": False, "reason": "that does not look like a full number"}

    # import asyncpg
    # pool = await get_pool()                       # keep a module-level pool;
    # async with pool.acquire() as connection:      # do not connect per call
    #     row = await connection.fetchrow(
    #         "SELECT plan, expires_at FROM subscriptions WHERE phone = $1", digits
    #     )
    # if row is None:
    #     return {"found": False}
    # return {
    #     "found": True,
    #     "plan": row["plan"],
    #     "expires": row["expires_at"].strftime("%d %B %Y"),
    # }
    raise NotImplementedError("wire this to your subscriber database")


# --------------------------------------------------------------------------
# 3. A SYNCHRONOUS tool.
#
#    Plain functions are fine; they run in a worker thread so they never block
#    the audio loop. Use this shape for a blocking driver you cannot await.
# --------------------------------------------------------------------------


@realtime_tool(
    name="device_setup_lookup",
    description="Get the setup steps for the device the caller says they will watch on.",
    params={
        "device": {
            "type": "string",
            "description": "The device the caller named, in their own words.",
        }
    },
    required=["device"],
    timeout_secs=1.0,
)
def device_setup_lookup(device: str) -> dict:
    steps = {
        "firestick": "Install the app from the Amazon Appstore, then sign in with the "
        "code we text you.",
        "smart tv": "Open your TV's app store, search for the app, and sign in with the "
        "code we text you.",
        "apple tv": "Install the app from the App Store, then sign in with the code we text you.",
        "android": "Install the app from Google Play, then sign in with the code we text you.",
    }
    asked = device.strip().lower()
    for name, instructions in steps.items():
        if name in asked:
            return {"found": True, "device": name, "steps": instructions}
    return {
        "found": False,
        "known_devices": sorted(steps),
        "say": "Ask which of the supported devices they have; do not invent steps.",
    }
