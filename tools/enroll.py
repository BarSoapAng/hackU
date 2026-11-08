"""Explicit operator command; it never starts the application or sends a message."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict

import httpx

from app.photon import PhotonClient, PhotonError


async def main() -> None:
    parser = argparse.ArgumentParser(description="Preassign a demo participant to Photon.")
    parser.add_argument("phone", help="E.164 participant number, including +country code")
    parser.add_argument("--operation-id", required=True, help="Stable unique key; preserve for this enrollment's retries")
    args = parser.parse_args()
    async with httpx.AsyncClient() as client:
        photon = PhotonClient(client, os.environ["PHOTON_PROJECT_ID"], os.environ["PHOTON_API_KEY"])
        try:
            assignment = await photon.enroll(args.phone, args.operation_id)
        except PhotonError as error:
            parser.exit(1, f"Enrollment did not complete: {error.code}\n")
        print(json.dumps(asdict(assignment)))
        print("Share the returned destination. The participant must text it after assignment before replies are eligible.")


if __name__ == "__main__":
    asyncio.run(main())
